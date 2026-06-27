"""
Sub-Agent 2 (Gemini brain) — Risk Diagnosis Engine

입력: TopologicalGraph (Sub-Agent 1 output)
출력: per-node risk vector (axis별 cell + risk score) — aggregator 호환 diagnosis dict

본질은 deterministic — ontology v0.4 36 cells lookup (sub_agent_5의 raw_cells_for_node 재활용).
Brain은 unmatched node (ontology cell 없는 노드) 에 LLM inference로 risk axis hint 보강.

[금지] sub_agent_5_mitigation.py 본문 수정 X — wrapper는 import만.
"""
from __future__ import annotations

from typing import Optional

from ._base import GeminiSubAgentBase
from ..sub_agent_5_mitigation import SubAgent5MitigationRecommender
from ...parser import TopologicalGraph


GEMINI_INFER_PROMPT_TPL = """\
You are an AI workflow risk analyst. Given a workflow node that is NOT in our curated risk
ontology, infer the likely risk axes (general_failure / security / handoff) it belongs to.

Node id      : {node_id}
Node label   : {node_label}
AI mode      : {ai_mode}
Color hint   : {color}

Output strict JSON with keys:
  primary_failure_mode: short snake_case string
  primary_threats: list of {{id, title}} (OWASP LLM Top 10 or "n/a")
  primary_handoff_risk: short snake_case string
  risk_score_general: float 0~5
  risk_score_security: float 0~5
  risk_score_handoff: float 0~5

Output ONLY the JSON, no prose.
"""


class SubAgent2RiskGemini(GeminiSubAgentBase):
    name = "sub_agent_2_risk_gemini"

    def __init__(self, brain=None, recommender: Optional[SubAgent5MitigationRecommender] = None):
        super().__init__(brain)
        # 기존 ontology lookup 인프라 재사용 — recommender의 raw_cells_for_node + diagnosis_dict_for_node
        self.recommender = recommender or SubAgent5MitigationRecommender()

    def diagnose_node(self, node, sample_source: str) -> dict:
        """Returns aggregator-compatible diagnosis dict."""
        diagnosis = self.recommender.diagnosis_dict_for_node(node, sample_source)
        cells_total = sum(len(v) for v in diagnosis["cells_by_axis"].values())
        if cells_total == 0 and not self.is_mock:
            # ontology에 없는 노드 — Gemini LLM inference로 risk axis hint emit
            llm_out = self.llm(
                GEMINI_INFER_PROMPT_TPL.format(
                    node_id=node.id, node_label=node.label,
                    ai_mode=node.ai_mode, color=node.color,
                ),
                fallback="",
            )
            diagnosis["llm_inferred"] = self._parse_llm_inference(llm_out)
        return diagnosis

    @staticmethod
    def _parse_llm_inference(text: str) -> dict:
        """LLM JSON parse — robust to fence wrapping. 실패 시 empty dict."""
        if not text:
            return {}
        import json, re
        # strip fences
        cleaned = re.sub(r"^```(?:json)?\s*|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return {"raw": text[:200], "parse_failed": True}

    def run(self, graph: TopologicalGraph, sample_source: str) -> list[dict]:
        """전체 graph → list of diagnosis dicts (RED + YELLOW 만)."""
        return [self.diagnose_node(n, sample_source) for n in graph.red_yellow_nodes()]
