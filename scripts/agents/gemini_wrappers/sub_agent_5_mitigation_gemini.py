"""
Sub-Agent 5 (Gemini brain) — Mitigation Recommender (compose 패턴)

[금지] scripts/agents/sub_agent_5_mitigation.py 본문 수정 X.
본 wrapper는 기존 SubAgent5MitigationRecommender를 import 후 compose:
  1. Recommender → NodeMitigationDossier (ontology 36 cells + Multi-Option)
  2. Gemini brain → MIT Mitigation Taxonomy (831 항목) 추가 매칭 + rationale 보강
  3. dossier dict에 'gemini_extras' field 추가

Mock fallback: gemini_extras = [] (ontology dossier 그대로).
"""
from __future__ import annotations

from typing import Optional

from ._base import GeminiSubAgentBase, load_yaml
from ..sub_agent_5_mitigation import (
    NodeMitigationDossier,
    SubAgent5MitigationRecommender,
)


GEMINI_MIT831_PROMPT_TPL = """\
You are matching a workflow failure to the MIT AI Mitigation Taxonomy (831 mitigations, 4 categories
× 23 subcategories).

Node id      : {node_id}
Failure mode : {failure_mode}
Existing Must Fix (from ontology):
  {existing_must_fix}

Suggest UP TO 3 additional mitigations from MIT taxonomy that are NOT already covered by the
existing Must Fix. Output strict JSON list:
  [{{category, subcategory, action_one_liner, rationale_one_liner}}, ...]

Output ONLY the JSON list. If nothing new, return [].
"""


class SubAgent5MitigationGemini(GeminiSubAgentBase):
    name = "sub_agent_5_mitigation_gemini"

    def __init__(self, brain=None, recommender: Optional[SubAgent5MitigationRecommender] = None):
        super().__init__(brain)
        self.recommender = recommender or SubAgent5MitigationRecommender()
        self._mit_taxonomy = None

    def _ensure_mit_loaded(self):
        if self._mit_taxonomy is None:
            self._mit_taxonomy = load_yaml("mit-mitigation-taxonomy.yaml")

    def recommend(self, node, sample_source: str, color: str = "RED") -> dict:
        dossier = self.recommender.diagnose_node(node.id, sample_source, color)
        result = dossier.to_dict()
        result["gemini_extras"] = self._extra_mitigations(dossier) if not self.is_mock else []
        result["brain"] = self.brain.name
        return result

    def _extra_mitigations(self, dossier: NodeMitigationDossier) -> list[dict]:
        self._ensure_mit_loaded()
        # extract existing must_fix actions (모든 axis 합산)
        existing = []
        primary_failure = ""
        for axis_cells in dossier.cells_by_axis.values():
            for c in axis_cells:
                if c.primary and not primary_failure:
                    primary_failure = c.primary
                for o in c.options:
                    if o.tier == "must_fix":
                        existing.append(f"- {o.action[:120]}")
        if not existing:
            return []
        llm_out = self.llm(
            GEMINI_MIT831_PROMPT_TPL.format(
                node_id=dossier.node_id,
                failure_mode=primary_failure or "unspecified",
                existing_must_fix="\n  ".join(existing[:5]),
            ),
            fallback="",
        )
        return self._safe_json_list(llm_out)

    @staticmethod
    def _safe_json_list(text: str) -> list[dict]:
        if not text:
            return []
        import json, re
        cleaned = re.sub(r"^```(?:json)?\s*|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []

    def run(self, graph, sample_source: str) -> list[dict]:
        return [self.recommend(n, sample_source, n.color) for n in graph.red_yellow_nodes()]
