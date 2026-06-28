"""
FDE Agent — Shared Core I/O Contracts
=====================================

★ Single source of truth for the diagnosis-core input/output schema that BOTH
orchestration layers import unchanged:

  - ADK / Rapid path  (external, Cloud Run)  → wraps core tools as ADK FunctionTools
  - CrewAI / UiPath path (multi-model, Coded Agent) → wraps core tools as CrewAI @tool

The contract is **brain-agnostic** on purpose. The deterministic diagnosis core
(ontology + RAG + 3-axis scoring) carries no model choice; the LLM brain is
selected by the orchestration layer (`brain_factory.get_brain(policy=...)`). This
is what structurally separates the Rapid-path brain policy from the UiPath path while letting
the SAME tools run under both stacks.

Shapes are plain dataclasses with `.to_dict()` so the result is JSON-safe for
UiPath Maestro / Action Center and FastAPI responses alike.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# =============================================================
# Input contract
# =============================================================

VALID_WORKFLOW_FORMATS = {"markdown_sample", "markdown_inline", "bpmn_xml", "mermaid"}

# Canonical ontology sample_source tags (cell filter). None = no filter (inline).
VALID_SAMPLE_SOURCES = {"legal", "korean_loan", None}


@dataclass
class WorkflowInput:
    """Normalized workflow input — the single shape every entry point converts to.

    content:        raw text of the workflow. For markdown_* formats this is the
                    Layer-2 markdown containing a '## Node Inventory' table.
                    For bpmn_xml / mermaid this is the raw diagram (Phase 1+ parser
                    converts to the markdown node table before scoring).
    workflow_format: one of VALID_WORKFLOW_FORMATS.
    sample_source:  ontology cell filter — "legal" | "korean_loan" | None.
    title:          human-readable workflow title for reports.
    sample_name:    optional convenience key ("legal" | "loan") when the caller
                    is running a bundled sample rather than supplying content.
    """
    content: str = ""
    workflow_format: str = "markdown_sample"
    sample_source: Optional[str] = None
    title: str = ""
    sample_name: Optional[str] = None

    def validate(self) -> None:
        if self.workflow_format not in VALID_WORKFLOW_FORMATS:
            raise ValueError(
                f"workflow_format must be one of {sorted(VALID_WORKFLOW_FORMATS)}, "
                f"got {self.workflow_format!r}"
            )
        if self.sample_source not in VALID_SAMPLE_SOURCES:
            raise ValueError(
                f"sample_source must be one of {sorted(s for s in VALID_SAMPLE_SOURCES if s)} or None, "
                f"got {self.sample_source!r}"
            )
        if not self.content and not self.sample_name:
            raise ValueError("WorkflowInput requires either content or sample_name")


@dataclass
class HitlThresholds:
    """Gateway thresholds — mirror of the Maestro Exclusive Gateway condition.

    Kept in the contract (not the wrapper) so the ADK path, the CrewAI path and
    the BPMN gateway all read the identical defaults.
    """
    confdecay_over_trust: float = 0.2
    ips_min: float = 0.5
    laaj_min: float = 0.6
    final_score_red: float = 4.5

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "HitlThresholds":
        base = cls()
        for k, v in (d or {}).items():
            if k not in base.__dataclass_fields__:
                raise ValueError(f"unknown hitl_threshold key {k!r}")
            if not isinstance(v, (int, float)):
                raise ValueError(f"hitl_threshold.{k} must be numeric, got {type(v).__name__}")
            setattr(base, k, float(v))
        return base


@dataclass
class DiagnosisOptions:
    """Pipeline toggles + HITL thresholds. Defaults reproduce diagnose.py v0.2."""
    include_handoff_metrics: bool = True
    include_aggregator: bool = True
    rag_top_n: int = 5
    laaj_backend: str = "mock"           # "mock" | "auto" (LLM-judge sampling) — UiPath path only
    hitl_thresholds: HitlThresholds = field(default_factory=HitlThresholds)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "DiagnosisOptions":
        d = d or {}
        return cls(
            include_handoff_metrics=bool(d.get("include_handoff_metrics", True)),
            include_aggregator=bool(d.get("include_aggregator", True)),
            rag_top_n=int(d.get("rag_top_n", 5)),
            laaj_backend=str(d.get("laaj_backend", "mock")),
            hitl_thresholds=HitlThresholds.from_dict(d.get("hitl_threshold")),
        )


# =============================================================
# Output contract
# =============================================================

@dataclass
class GraphSummary:
    nodes: list = field(default_factory=list)        # all parsed nodes (heatmap rows)
    n_nodes: int = 0
    n_red: int = 0
    ai_intervention_nodes: int = 0                    # nodes with non-"Untouched" ai_mode
    edges: list = field(default_factory=list)         # [[src_id, dst_id], ...] full diagram topology (front graph layout)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiagnosisResult:
    """Canonical diagnosis-core output. JSON-safe via to_dict().

    This is what:
      - coded_agent_wrapper.run() returns to UiPath Maestro (hitl_required gate)
      - serve/app.run_diagnosis() returns to FastAPI
      - a CrewAI / ADK orchestrator collects after running the tool chain
    """
    status: str = "ok"                                # "ok" | "error"
    workflow: str = ""
    degraded: bool = False
    notes: list = field(default_factory=list)
    graph: GraphSummary = field(default_factory=GraphSummary)
    diagnoses: list = field(default_factory=list)     # list[AggregatedNode.to_dict()]
    metrics: dict = field(default_factory=dict)       # {ips/confdecay/laaj: {rows, alerts}}
    max_final_score: float = 0.0
    runtime_alerts: int = 0
    hitl_required: bool = False
    hitl_reason: str = ""
    thresholds_applied: dict = field(default_factory=dict)
    brain: dict = field(default_factory=dict)         # orchestrator-supplied brain healthcheck (optional)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        out = asdict(self)
        out["graph"] = self.graph.to_dict() if isinstance(self.graph, GraphSummary) else self.graph
        return out


__all__ = [
    "VALID_WORKFLOW_FORMATS",
    "VALID_SAMPLE_SOURCES",
    "WorkflowInput",
    "HitlThresholds",
    "DiagnosisOptions",
    "GraphSummary",
    "DiagnosisResult",
]
