"""
Sub-Agent 3 (Gemini brain) — Standards Mapping (OWASP / MITRE ATLAS / NIST / EU AI Act)

입력: diagnosis dict (Sub-Agent 2 output) 또는 Node
출력: per-node standards mapping
  - OWASP LLM Top 10 v2025 (이미 ontology cells의 primary_threats에 박혀있음)
  - MITRE ATLAS techniques (ontology cells에 부분 박혀있음)
  - NIST AI RMF (ontology에 미부착 — YAML lookup)
  - EU AI Act Annex III (ontology에 미부착 — YAML lookup)

Brain: cell에 명시 안 된 표준에 대해 LLM이 추가 매칭 제안.
Mock fallback: ontology + YAML lookup만 사용 (LLM 보강 skip).
"""
from __future__ import annotations

from typing import Optional

from ._base import GeminiSubAgentBase, load_yaml


GEMINI_MAP_PROMPT_TPL = """\
Given a workflow node and its detected risk, suggest mappings to:
  - NIST AI RMF (Govern/Map/Measure/Manage 4 functions + subcategories)
  - EU AI Act Annex III (high-risk system categories 1~8)

Node id      : {node_id}
Failure mode : {failure_mode}
AI mode      : {ai_mode}

Output strict JSON:
  nist_ai_rmf: list of {{function, subcategory}}
  eu_ai_act:   list of {{annex_iii_category, rationale_one_liner}}

Output ONLY the JSON. If unsure, return empty lists.
"""


class SubAgent3StandardsGemini(GeminiSubAgentBase):
    name = "sub_agent_3_standards_gemini"

    def __init__(self, brain=None):
        super().__init__(brain)
        # YAML lazy load
        self._owasp = None
        self._mitre = None
        self._nist = None
        self._eu = None

    def _ensure_loaded(self):
        if self._owasp is None:
            self._owasp = load_yaml("owasp-llm-top10-v2025.yaml")
            self._mitre = load_yaml("mitre-atlas.yaml")
            self._nist = load_yaml("nist-ai-rmf-v0.1.yaml")
            self._eu = load_yaml("eu-ai-act-annex-iii-v0.1.yaml")

    def map_node(self, diagnosis: dict) -> dict:
        """diagnosis dict (Sub-Agent 2 output) → standards mapping dict."""
        self._ensure_loaded()
        node = diagnosis.get("node", {})
        cells_by = diagnosis.get("cells_by_axis", {})

        # 1) ontology cells에 이미 박힌 OWASP/MITRE 그대로 collect
        owasp_hits = []
        mitre_hits = []
        for cells in cells_by.values():
            for c in cells:
                for t in c.get("primary_threats", []) or []:
                    if t.get("id", "").startswith("LLM"):
                        owasp_hits.append({"id": t["id"], "title": t.get("title", ""), "relevance": t.get("relevance", "")})
                for m in (c.get("mitre_atlas_techniques", []) or []) + (c.get("mitre_atlas_tactics", []) or []):
                    mitre_hits.append({"id": m.get("id", ""), "title": m.get("title", "")})

        # 2) NIST / EU는 LLM이 추론 (ontology에 미부착)
        nist_hits = []
        eu_hits = []
        if not self.is_mock:
            failure_mode = (
                next((c.get("primary_failure_mode") for cells in cells_by.values() for c in cells if c.get("primary_failure_mode")), "")
                or next((c.get("primary_handoff_risk") for cells in cells_by.values() for c in cells if c.get("primary_handoff_risk")), "")
                or "unspecified"
            )
            llm_out = self.llm(
                GEMINI_MAP_PROMPT_TPL.format(
                    node_id=node.get("id", "?"),
                    failure_mode=failure_mode,
                    ai_mode=node.get("ai_mode", "?"),
                ),
                fallback="",
            )
            parsed = self._safe_json(llm_out)
            nist_hits = parsed.get("nist_ai_rmf", []) or []
            eu_hits = parsed.get("eu_ai_act", []) or []

        return {
            "node_id": node.get("id"),
            "owasp_llm_top10": _dedup(owasp_hits, key="id"),
            "mitre_atlas": _dedup(mitre_hits, key="id"),
            "nist_ai_rmf": nist_hits,
            "eu_ai_act": eu_hits,
            "source": "ontology+llm" if not self.is_mock else "ontology_only",
        }

    @staticmethod
    def _safe_json(text: str) -> dict:
        if not text:
            return {}
        import json, re
        cleaned = re.sub(r"^```(?:json)?\s*|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {}

    def run(self, diagnoses: list[dict]) -> list[dict]:
        return [self.map_node(d) for d in diagnoses]


def _dedup(items: list[dict], key: str) -> list[dict]:
    seen = set()
    out = []
    for it in items:
        k = it.get(key)
        if k and k not in seen:
            seen.add(k)
            out.append(it)
    return out
