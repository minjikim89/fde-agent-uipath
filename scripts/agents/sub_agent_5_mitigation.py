"""
FDE Agent — Sub-Agent 5: Mitigation Recommender (v0.1)

본 모듈은 mapping-ontology-v0.1.yaml (v0.3c, 36 cells) 위에 작동.
RED 노드 input → axis별 cell lookup → Multi-Option mitigation (Must Fix / Recommend / Optional) + 5차원 trade-off score (0~5) emit.

5차원 trade-off:
  - risk_delta        (negative = risk 감소량의 absolute 크기, 0~5)
  - cost              (운영·구현 비용, 1=낮음 5=높음)
  - speed_delta       (throughput 영향, 1=무시 5=큰 영향)
  - op_complexity     (운영 복잡도, 1=낮음 5=높음)
  - impl_effort       (구현 effort, 1=낮음 5=높음)

한국 컨텍스트 우선:
  - loan 노드: sample_source=korean_loan cells 우선 retrieval (KoFIU / 공정대출법 / K-PIPA)
  - legal 노드: sample_source=legal cells 우선

설계 결정 (ontology cell schema 정합 유지):
  - mitigation_options 의 (must_fix / recommend / optional) key 그대로 emit
  - 각 옵션에 action / rationale / scores 첨부
  - heuristic_source / risk_score 등 ontology field 보존 → audit trail 가능
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import yaml


# 기본 ontology 경로 (repo 상대)
DEFAULT_ONTOLOGY = Path(__file__).parent.parent / "data" / "mapping-ontology-v0.1.yaml"


AXES = ("general_failure", "security", "handoff")

# 한국 컨텍스트 우선 키워드 (한국 영업 wedge 강화)
KOREAN_PRIORITY_KEYWORDS = (
    "KoFIU", "K-PIPA", "공정대출법", "신용정보법", "외환관리법", "자금세탁",
    "보이스피싱", "오픈뱅킹", "주민등록", "한글", "KCB", "NICE", "금감원",
    "한국", "Korean",
)


# =============================================================
# Trade-off scoring heuristics
# =============================================================
#
# ontology에 정량값이 없으므로, tier (must_fix/recommend/optional) 기본값 +
# action 텍스트 keyword로 보정. 추후 sample markdown의 실제 trade-off matrix
# (legal v0.2 § Multi-Option Mitigations)를 ground-truth로 fine-tune 가능.

_TIER_BASELINE = {
    "must_fix":  dict(risk_delta=3.0, cost=2, speed_delta=2, op_complexity=2, impl_effort=2),
    "recommend": dict(risk_delta=2.0, cost=3, speed_delta=3, op_complexity=3, impl_effort=3),
    "optional":  dict(risk_delta=1.0, cost=4, speed_delta=2, op_complexity=4, impl_effort=4),
}

# action 텍스트 keyword에 따른 ± 보정 (각 차원별 nudge)
_KEYWORD_NUDGES: list[tuple[str, dict]] = [
    # 비용·effort up
    ("ensemble",            dict(cost=+1, impl_effort=+1, risk_delta=+0.5)),
    ("fine-tune",           dict(cost=+1, impl_effort=+2, risk_delta=+0.5)),
    ("retrain",             dict(cost=+1, impl_effort=+1)),
    ("differential privacy",dict(cost=+1, impl_effort=+2, op_complexity=+1)),
    ("durable workflow",    dict(impl_effort=+1, op_complexity=+1)),
    ("federated",           dict(cost=+1, impl_effort=+2, op_complexity=+2)),
    ("multi-factor",        dict(cost=+1, impl_effort=+1, risk_delta=+0.5)),
    # 사람 개입 → cost+speed
    ("attorney",            dict(cost=+1, speed_delta=+1)),
    ("심사역",              dict(cost=+1, speed_delta=+1)),
    ("변호사",              dict(cost=+1, speed_delta=+1)),
    ("human", dict(cost=+1, speed_delta=+1)),
    ("HITL",  dict(cost=+1, speed_delta=+1)),
    # gating / threshold → low cost·effort + risk Δ↑
    ("threshold",           dict(risk_delta=+0.5, op_complexity=-1, impl_effort=-1)),
    ("gating",              dict(risk_delta=+0.5, impl_effort=-1)),
    ("template enforcement",dict(risk_delta=+1.0, impl_effort=-1)),
    ("sign-off",            dict(risk_delta=+0.5, cost=+1)),
    # audit / monitoring / sample → low risk Δ
    ("audit",               dict(risk_delta=-0.5, op_complexity=+1)),
    ("monitor",             dict(risk_delta=-0.5)),
    ("sample",              dict(risk_delta=-0.5, cost=-1)),
    ("dashboard",           dict(impl_effort=+1, op_complexity=+1)),
    # 한국 규제 컨텍스트 → risk Δ↑ (compliance value 큼)
    ("K-PIPA",              dict(risk_delta=+0.5)),
    ("공정대출법",          dict(risk_delta=+0.5)),
    ("KoFIU",               dict(risk_delta=+0.5)),
    ("금감원",              dict(risk_delta=+0.5)),
]


# =============================================================
# Data classes (HTML renderer + e2e wiring과 호환)
# =============================================================

@dataclass
class MitigationOption:
    tier: str                       # must_fix / recommend / optional
    action: str
    rationale: str
    risk_delta: float               # 0~5 (감소량 크기)
    cost: int                       # 1~5
    speed_delta: int                # 1~5 (throughput 영향)
    op_complexity: int              # 1~5
    impl_effort: int                # 1~5

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CellDiagnosis:
    cell_id: str
    axis: str
    risk_score: float | None
    primary: str                    # primary failure / threat / handoff risk
    description: str
    evidence: dict                  # aiid_incidents / academic / threats (axis-specific)
    heuristic_source: str | None
    options: list[MitigationOption] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "axis": self.axis,
            "risk_score": self.risk_score,
            "primary": self.primary,
            "description": self.description,
            "evidence": self.evidence,
            "heuristic_source": self.heuristic_source,
            "options": [o.to_dict() for o in self.options],
        }


@dataclass
class NodeMitigationDossier:
    node_id: str
    sample_source: str              # legal / korean_loan
    color: str                      # RED / YELLOW
    aggregate_risk: float           # axis별 risk_score 평균
    cells_by_axis: dict[str, list[CellDiagnosis]] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)

    def all_options(self) -> list[MitigationOption]:
        out: list[MitigationOption] = []
        for axis_cells in self.cells_by_axis.values():
            for c in axis_cells:
                out.extend(c.options)
        return out

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "sample_source": self.sample_source,
            "color": self.color,
            "aggregate_risk": self.aggregate_risk,
            "cells_by_axis": {
                a: [c.to_dict() for c in cells] for a, cells in self.cells_by_axis.items()
            },
            "summary": self.summary,
        }


# =============================================================
# Core recommender
# =============================================================

class SubAgent5MitigationRecommender:
    """
    Lookup-based recommender. ontology v0.3c의 mitigation_options 그대로 emit + 5차원 score 부여.
    """

    def __init__(self, ontology_path: str | Path | None = None):
        self.ontology_path = Path(ontology_path) if ontology_path else DEFAULT_ONTOLOGY
        self._load_ontology()

    def _load_ontology(self):
        """Direct YAML load — root fix landed in mapping-ontology-v0.1.yaml (v0.3c reconcile)."""
        with open(self.ontology_path, encoding="utf-8") as f:
            self.ontology = yaml.safe_load(f) or {}
        self.cells: list[dict] = self.ontology.get("cells", []) or []
        self.version: str = self.ontology.get("version", "unknown")

    # ---------- public API ----------

    def diagnose_node(self, node_id: str, sample_source: str, color: str = "RED") -> NodeMitigationDossier:
        """
        Single node → NodeMitigationDossier.
        sample_source: 'legal' or 'korean_loan'. 한국 컨텍스트 우선 retrieval은
        sample_source 기반.
        """
        cells = self._cells_for_node(node_id, sample_source)
        cells_by_axis: dict[str, list[CellDiagnosis]] = {a: [] for a in AXES}
        risk_scores: list[float] = []
        for c in cells:
            axis = c.get("axis", "general_failure")
            if axis not in cells_by_axis:
                cells_by_axis[axis] = []
            diag = self._cell_to_diagnosis(c)
            cells_by_axis[axis].append(diag)
            if diag.risk_score is not None:
                risk_scores.append(float(diag.risk_score))

        aggregate = round(sum(risk_scores) / len(risk_scores), 2) if risk_scores else 0.0

        dossier = NodeMitigationDossier(
            node_id=node_id,
            sample_source=sample_source,
            color=color,
            aggregate_risk=aggregate,
            cells_by_axis=cells_by_axis,
            summary=self._summarize(cells_by_axis, aggregate, sample_source),
        )
        return dossier

    def diagnose_nodes(self, nodes: Iterable[tuple[str, str, str]]) -> list[NodeMitigationDossier]:
        """nodes = iterable of (node_id, sample_source, color)."""
        return [self.diagnose_node(nid, src, col) for nid, src, col in nodes]

    def raw_cells_for_node(self, node_id: str, sample_source: str) -> list[dict]:
        """
        Aggregator (scripts/agents/aggregator.py)는 raw ontology cell dict 형태를 input으로
        받는다 (primary_failure_mode / primary_handoff_risk / primary_threats 등 ontology field).
        본 헬퍼는 graph node id + sample_source → 매칭 raw cells을 그대로 노출 — aggregator wiring 용.
        """
        return self._cells_for_node(node_id, sample_source)

    def diagnosis_dict_for_node(self, node, sample_source: str) -> dict:
        """
        aggregator.aggregate_node()가 기대하는 diagnosis dict shape으로 변환.
        node: parser.Node (id, label, ai_mode 등)
        """
        raw_cells = self.raw_cells_for_node(node.id, sample_source)
        cells_by_axis: dict[str, list[dict]] = {a: [] for a in AXES}
        for c in raw_cells:
            axis = c.get("axis", "general_failure")
            cells_by_axis.setdefault(axis, []).append(c)
        return {
            "node": {"id": node.id, "function": node.label, "ai_mode": node.ai_mode},
            "cells_by_axis": cells_by_axis,
            "aiid": [],
        }

    # ---------- cell lookup ----------

    def _cells_for_node(self, node_id: str, sample_source: str) -> list[dict]:
        """
        node_id 매칭 + sample_source 우선 retrieval.
        ontology cell의 `node` field는 형태가 다양함:
          - 'N2_clause_extraction', 'N5a_auto_approve' (legal)
          - 'N6_llm_risk_analysis', 'loan_N6_handoff' (loan)
        매칭 규칙:
          1. node id가 cell.node 의 leading id token과 일치
          2. sample_source field가 일치하거나 (없으면 default 'legal')
        한국 우선:
          - sample_source='korean_loan' 요청 시: korean_loan cells만 (없으면 fallback unspecified)
          - 'legal' 요청 시: legal cells (sample_source 없으면 default legal)
        """
        target_id = self._normalize_node_id(node_id)
        out: list[dict] = []
        for c in self.cells:
            cn = c.get("node", "")
            cn_id = self._extract_cell_node_id(cn)
            if cn_id != target_id:
                continue
            cs = c.get("sample_source", "legal")  # ontology default per v0.1
            if cs != sample_source:
                continue
            out.append(c)

        # 정렬: axis 우선순위 (general_failure → security → handoff) — 한국 컨텍스트는
        # 통상 handoff에 더 강하므로 마지막에 spotlight되도록 (presentation order)
        axis_rank = {a: i for i, a in enumerate(AXES)}
        out.sort(key=lambda c: axis_rank.get(c.get("axis", ""), 99))

        # Korean 우선 — 동일 axis 내에서는 한국 키워드가 description에 있는 cell을 위로
        if sample_source == "korean_loan":
            out.sort(key=lambda c: 0 if self._has_korean_keyword(c) else 1)
        return out

    @staticmethod
    def _extract_cell_node_id(cell_node_field: str) -> str:
        """
        Ontology cell.node field → graph node id 형태로 정규화.

        예시:
          'N2_clause_extraction'      → 'N2'
          'N2_eKYC'                   → 'N2'   (mixed-case suffix OK)
          'N3_AML_screening'          → 'N3'   (uppercase token suffix OK)
          'N4_credit_scoring_ACS'     → 'N4'   (trailing uppercase token OK)
          'N5a_auto_approve'          → 'N5A'  (numeric+alpha id 보존)
          'loan_N6_handoff'           → 'N6'
        규칙: 'loan_' prefix 제거 + 첫 '_' 전 토큰만 추출. case-insensitive.
        """
        if not cell_node_field:
            return ""
        s = cell_node_field
        if s.startswith("loan_"):
            s = s[len("loan_"):]
        first_token = s.split("_", 1)[0]
        return first_token.upper()

    @staticmethod
    def _normalize_node_id(node_id: str) -> str:
        """parser의 node id (e.g., 'N2', 'D1')를 cell lookup용으로 normalize."""
        s = node_id.strip()
        if s.startswith("loan_"):
            s = s[len("loan_"):]
        first_token = s.split("_", 1)[0]
        return first_token.upper()

    @staticmethod
    def _has_korean_keyword(cell: dict) -> bool:
        blob = str(cell)
        return any(k.lower() in blob.lower() for k in KOREAN_PRIORITY_KEYWORDS)

    # ---------- cell → diagnosis ----------

    def _cell_to_diagnosis(self, cell: dict) -> CellDiagnosis:
        axis = cell.get("axis", "general_failure")
        risk_score = cell.get("risk_score")
        primary = self._cell_primary(cell)
        description = self._cell_description(cell)
        evidence = self._cell_evidence(cell)
        heuristic_source = cell.get("heuristic_source")
        options = self._cell_mitigation_options(cell)
        return CellDiagnosis(
            cell_id=cell.get("cell_id", "unknown"),
            axis=axis,
            risk_score=risk_score,
            primary=primary,
            description=description,
            evidence=evidence,
            heuristic_source=heuristic_source,
            options=options,
        )

    @staticmethod
    def _cell_primary(cell: dict) -> str:
        return (
            cell.get("primary_failure_mode")
            or cell.get("primary_handoff_risk")
            or (cell.get("primary_threats") or [{}])[0].get("title", "")
            or ""
        )

    @staticmethod
    def _cell_description(cell: dict) -> str:
        desc = cell.get("description") or ""
        return desc.strip()

    @staticmethod
    def _cell_evidence(cell: dict) -> dict:
        ev = {}
        if "evidence" in cell:
            ev["aiid_incidents"] = cell["evidence"].get("aiid_incidents", [])
            ev["academic"] = cell["evidence"].get("academic", [])
        if "primary_threats" in cell:
            ev["primary_threats"] = cell["primary_threats"]
        if "mitre_atlas_techniques" in cell:
            ev["mitre_atlas_techniques"] = cell["mitre_atlas_techniques"]
        if "mitre_atlas_tactics" in cell:
            ev["mitre_atlas_tactics"] = cell["mitre_atlas_tactics"]
        return ev

    def _cell_mitigation_options(self, cell: dict) -> list[MitigationOption]:
        """
        cell.mitigation_options (must_fix/recommend/optional) → MitigationOption × N.
        ontology cell에 mitigation_options가 없으면 (mitigation_options_ref만 있으면)
        cell.risk_score 기반 placeholder option 1건 emit (해당 셀의 ontology refresh가
        outstanding함을 표시).
        """
        mit = cell.get("mitigation_options", {}) or {}
        out: list[MitigationOption] = []
        for tier in ("must_fix", "recommend", "optional"):
            spec = mit.get(tier)
            if not spec:
                continue
            action = spec.get("action", "")
            rationale = spec.get("rationale", "")
            scores = self._compute_scores(tier, action, cell)
            out.append(MitigationOption(
                tier=tier,
                action=action,
                rationale=rationale,
                **scores,
            ))
        if not out and cell.get("mitigation_options_ref"):
            # cell schema 정합 — placeholder
            ref = cell["mitigation_options_ref"]
            out.append(MitigationOption(
                tier="must_fix",
                action=f"(see external reference: {ref})",
                rationale="ontology cell delegates mitigation detail to sample markdown — full options emitted in sample reference",
                risk_delta=2.5, cost=2, speed_delta=2, op_complexity=2, impl_effort=2,
            ))
        return out

    @staticmethod
    def _compute_scores(tier: str, action: str, cell: dict) -> dict:
        """
        tier baseline + action keyword nudge로 5차원 score 도출.
        cell의 risk_score가 높을수록 must_fix의 risk_delta↑ (high-risk cell 강한 mitigation 가정).
        """
        base = dict(_TIER_BASELINE.get(tier, _TIER_BASELINE["recommend"]))

        # cell risk_score 기반 risk_delta 보정
        risk = cell.get("risk_score")
        if isinstance(risk, (int, float)):
            if risk >= 4.5 and tier == "must_fix":
                base["risk_delta"] = max(base["risk_delta"], 3.5)
            elif risk <= 3.0 and tier == "must_fix":
                base["risk_delta"] = min(base["risk_delta"], 2.0)

        # keyword nudge
        haystack = (action or "").lower()
        for kw, nudges in _KEYWORD_NUDGES:
            if kw.lower() in haystack:
                for dim, delta in nudges.items():
                    base[dim] = base[dim] + delta

        # clamp 0~5
        for k in ("cost", "speed_delta", "op_complexity", "impl_effort"):
            base[k] = max(1, min(5, int(round(base[k]))))
        base["risk_delta"] = round(max(0.0, min(5.0, float(base["risk_delta"]))), 1)
        return base

    # ---------- summary ----------

    @staticmethod
    def _summarize(cells_by_axis: dict[str, list[CellDiagnosis]], aggregate: float, sample_source: str) -> dict:
        total_options = sum(len(c.options) for axis_cells in cells_by_axis.values() for c in axis_cells)
        axes_covered = sum(1 for cells in cells_by_axis.values() if cells)
        return {
            "aggregate_risk": aggregate,
            "axes_covered": axes_covered,
            "total_options": total_options,
            "primary_axis": max(
                cells_by_axis.items(),
                key=lambda kv: max((c.risk_score or 0) for c in kv[1]) if kv[1] else 0,
                default=("general_failure", []),
            )[0] if any(cells_by_axis.values()) else None,
            "korean_context": sample_source == "korean_loan",
        }


if __name__ == "__main__":
    # demo — Legal RED 3 + loan RED 3
    rec = SubAgent5MitigationRecommender()
    print(f"ontology version = {rec.version}, cells = {len(rec.cells)}")
    test_cases = [
        ("N2", "legal", "RED"),
        ("N3", "legal", "RED"),
        ("N5a", "legal", "RED"),
        ("N6", "korean_loan", "RED"),
        ("N7", "korean_loan", "RED"),
        ("N9", "korean_loan", "RED"),
    ]
    for nid, src, col in test_cases:
        d = rec.diagnose_node(nid, src, col)
        print(f"\n=== {src}:{nid} ({col}) aggregate_risk={d.aggregate_risk} ===")
        for axis, cells in d.cells_by_axis.items():
            for c in cells:
                opt_str = ", ".join(f"{o.tier}(Δrisk={o.risk_delta})" for o in c.options)
                print(f"  [{axis}] {c.cell_id} risk={c.risk_score} → {opt_str}")
