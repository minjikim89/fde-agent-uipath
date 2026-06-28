"""
FDE Agent — Diagnosis Engine (resource owner + pipeline orchestrator)
=====================================================================

`DiagnosisEngine` replaces diagnose.py's module-level globals (`ontology`,
`cells`, `model`, `inc_col`) with instance state and explicit lifecycle:

    engine = DiagnosisEngine()            # loads ontology (cheap, always)
    result = engine.diagnose(wf, opts)    # lazily loads RAG iff corpus present

It is the ONE place chromadb / sentence-transformers get imported, and only
inside `_ensure_rag()` — so importing the engine in a lint / UiPath-pack / docs
environment stays light. RAG is optional: if the corpus is absent the engine
degrades to ontology-only (`degraded=True`), matching serve/app.py behaviour.

Both orchestration layers use the engine the same way:
    - Rapid / ADK : Cloud Run constructs the engine once, an external brain is applied
                    by the serve layer for the executive summary.
    - UiPath/CrewAI: the Coded Agent constructs the engine; a multi-model brain
                    may be applied at the orchestration layer (UiPath path only).

The engine is brain-agnostic — the deterministic ontology+RAG+scoring core needs
no LLM. That is what keeps the SAME engine valid under both model policies.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from . import contracts
from .contracts import DiagnosisOptions, DiagnosisResult, GraphSummary, WorkflowInput
from . import tools


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SCRIPTS_DIR / "data"
DEFAULT_ONTOLOGY = DATA_DIR / "mapping-ontology-v0.1.yaml"
DEFAULT_CHROMA = DATA_DIR / "chroma"
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"

SAMPLE_DIR = DATA_DIR / "sample-workflows"
SAMPLE_FILES = {
    "legal": SAMPLE_DIR / "legal-contract-review-v0.1.md",
    "loan":  SAMPLE_DIR / "loan-underwriting-kr-v0.1.md",
}
SAMPLE_SOURCE = {"legal": "legal", "loan": "korean_loan"}
SAMPLE_TITLES = {
    "legal": "Vendor Contract Review (Layer 2 sample v0.2)",
    "loan": "Korean Personal Loan Underwriting (Layer 2 sample v0.2, AML+ACS)",
}


class DiagnosisEngine:
    """Owns ontology + (lazy) RAG resources; runs the full diagnosis pipeline."""

    def __init__(
        self,
        ontology_path: Path = DEFAULT_ONTOLOGY,
        chroma_path: Path = DEFAULT_CHROMA,
        embed_model: str = DEFAULT_EMBED_MODEL,
    ) -> None:
        self.ontology_path = Path(ontology_path)
        self.chroma_path = Path(chroma_path)
        self.embed_model_name = embed_model

        with open(self.ontology_path) as f:
            self.ontology: dict = yaml.safe_load(f)
        self.cells: list = self.ontology.get("cells", [])

        # RAG resources — populated lazily by _ensure_rag()
        self._model = None
        self._collection = None
        self._rag_error: Optional[str] = None

    # ----- RAG lifecycle (the only heavy imports live here) ----------------

    @property
    def rag_available(self) -> bool:
        return self.chroma_path.exists()

    def _ensure_rag(self) -> bool:
        """Lazily load BGE-M3 + Chroma collection. Returns True iff ready.
        Never raises — sets self._rag_error and returns False on failure."""
        if self._model is not None and self._collection is not None:
            return True
        if not self.rag_available:
            self._rag_error = "AIID corpus absent (.gitignore) — ontology-only diagnosis"
            return False
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer
            import torch

            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = SentenceTransformer(self.embed_model_name, device=device)
            client = chromadb.PersistentClient(path=str(self.chroma_path))
            self._collection = client.get_collection("aiid_incidents")
            return True
        except Exception as e:  # corpus/dep missing → degrade
            self._rag_error = f"RAG unavailable ({type(e).__name__}) — ontology-only diagnosis"
            self._model = None
            self._collection = None
            return False

    def embed_fn(self, text: str) -> list:
        """BGE-M3 embedding (normalized). Requires _ensure_rag() to have succeeded."""
        return self._model.encode([text], normalize_embeddings=True).tolist()[0]

    # ----- input resolution ------------------------------------------------

    @staticmethod
    def from_sample(sample_name: str) -> WorkflowInput:
        """Build a WorkflowInput from a bundled sample key ("legal"|"loan")."""
        if sample_name not in SAMPLE_FILES:
            raise ValueError(f"sample_name must be one of {list(SAMPLE_FILES)}, got {sample_name!r}")
        content = SAMPLE_FILES[sample_name].read_text(encoding="utf-8")
        return WorkflowInput(
            content=content,
            workflow_format="markdown_sample",
            sample_source=SAMPLE_SOURCE[sample_name],
            title=SAMPLE_TITLES[sample_name],
            sample_name=sample_name,
        )

    def _resolve_input(self, wf: WorkflowInput) -> WorkflowInput:
        if not wf.content and wf.sample_name:
            return self.from_sample(wf.sample_name)
        return wf

    # ----- main pipeline ---------------------------------------------------

    def diagnose(self, wf: WorkflowInput, options: Optional[DiagnosisOptions] = None) -> DiagnosisResult:
        """Run the full ontology + (optional) RAG + 3-axis scoring pipeline.

        Returns the canonical DiagnosisResult (JSON-safe via .to_dict()).
        """
        options = options or DiagnosisOptions()
        try:
            wf = self._resolve_input(wf)
            wf.validate()
        except ValueError as e:
            return DiagnosisResult(status="error", error=str(e))

        notes: list[str] = []
        degraded = False

        nodes = tools.parse_workflow(wf.content)
        if not nodes:
            degraded = True
            notes.append(
                "0 nodes parsed — expected a Markdown '## Node Inventory' table "
                "(raw BPMN XML is not yet auto-converted)."
            )
        # Candidate nodes = those the ONTOLOGY has risk cells for. Which nodes are
        # RED/AMBER is then decided by the ontology SCORE (below) — NOT read from the
        # input. (Previously this gated on parse_workflow's predicted_color, i.e. the
        # input's own "Expected Diagnosis Result" column, which let an input pre-label its answers.)
        by_axis_by_node = {
            n["id"]: tools.ontology_lookup(n["id"], self.cells, sample_source=wf.sample_source)
            for n in nodes
        }
        red = [n for n in nodes if any(by_axis_by_node[n["id"]].values())]

        # --- optional RAG: retrieve AIID incidents per RED node ---
        rag_by_node: dict[str, list] = {}
        rag_ready = self._ensure_rag()
        if not rag_ready and self._rag_error:
            degraded = True
            notes.append(self._rag_error)
        if rag_ready:
            for n in red:
                q = f"{n['function']} hallucination edge case failure"
                try:
                    rag_by_node[n["id"]] = tools.retrieve_incidents(
                        q, self.embed_fn, self._collection, n=options.rag_top_n
                    )
                except Exception as e:
                    degraded = True
                    notes.append(f"RAG query failed ({type(e).__name__}) — ontology-only for {n['id']}")

        # --- per-RED diagnosis: ontology cells (3 axes) + AIID evidence ---
        diagnoses = []
        for n in red:
            diagnoses.append({
                "node": n,
                "cells_by_axis": by_axis_by_node[n["id"]],
                "aiid": rag_by_node.get(n["id"], []),
            })

        # --- handoff metrics (IPS / ConfDecay / LaaJ) ---
        metric_rows: list = []
        handoff_by_dn: dict[str, list] = {}
        if options.include_handoff_metrics and rag_ready:
            metric_rows = tools.compute_handoff_metrics(
                diagnoses, wf.sample_source, self.embed_fn, laaj_backend=options.laaj_backend
            )
            for row in metric_rows:
                dn = row["pair"]["downstream_id"]
                handoff_by_dn.setdefault(dn, []).append(row)
        elif options.include_handoff_metrics and not rag_ready:
            notes.append("handoff metrics skipped — embedding model unavailable (ontology-only)")

        # --- aggregate: per-node final risk score ---
        aggregated = []
        if options.include_aggregator:
            aggregated = tools.aggregate_workflow(diagnoses, handoff_by_dn)

        # --- HITL gate (structured, no markdown regex) ---
        hitl_required, hitl_reason = tools.evaluate_hitl(
            aggregated, metric_rows, options.hitl_thresholds
        )
        max_final = max((a.final_score for a in aggregated), default=0.0)
        metrics_dict = tools.metrics_to_dict(metric_rows)
        runtime_alerts = sum(metrics_dict[k]["alerts"] for k in metrics_dict)

        # Node color + RED count come from the ontology SCORE (aggregated), not the
        # input's predicted_color — so the graph reflects the tool's verdict, not the input.
        _score_by_id = {a.node_id: a.final_score for a in aggregated}

        def _color_for(nid: str) -> str:
            s = _score_by_id.get(nid)
            if s is None:
                return "GREEN"
            return "RED" if s >= 4.0 else ("YELLOW" if s >= 2.5 else "GREEN")

        graph = GraphSummary(
            nodes=[{
                "id": n["id"], "label": n.get("label", n["id"]), "function": n["function"],
                "color": _color_for(n["id"]), "ai_mode": n["ai_mode"][:80],
            } for n in nodes],
            n_nodes=len(nodes),
            n_red=sum(1 for s in _score_by_id.values() if s >= 4.0),
            ai_intervention_nodes=tools.count_ai_intervention(nodes),
            edges=tools.extract_edges(wf.content),
        )

        # Aggregated dicts + per-node AIID evidence passthrough (BFF needs the real
        # retrieved incidents; aggregator does not carry them). Additive key.
        out_diagnoses = [a.to_dict() for a in aggregated]
        for od in out_diagnoses:
            od["aiid"] = rag_by_node.get(od["node_id"], [])

        return DiagnosisResult(
            status="ok",
            workflow=wf.title or wf.sample_name or "Inline workflow",
            degraded=degraded,
            notes=notes,
            graph=graph,
            diagnoses=out_diagnoses,
            metrics=metrics_dict,
            max_final_score=round(max_final, 2),
            runtime_alerts=runtime_alerts,
            hitl_required=hitl_required,
            hitl_reason=hitl_reason,
            thresholds_applied=options.hitl_thresholds.to_dict(),
        )


__all__ = ["DiagnosisEngine", "SAMPLE_FILES", "SAMPLE_SOURCE", "SAMPLE_TITLES"]
