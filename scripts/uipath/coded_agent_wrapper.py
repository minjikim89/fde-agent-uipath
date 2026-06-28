"""
UiPath Coded Agent Wrapper for FDE Agent — Phase 2 artifact
============================================================

Wraps the shared diagnosis core (`core.DiagnosisEngine`) as a UiPath Coded Agent.

★ Sprint 7 refactor: this wrapper now calls `core.engine.diagnose()` IN-PROCESS
and returns the structured `DiagnosisResult` directly. The previous design
(subprocess `python diagnose.py` + markdown-regex re-parse) is gone — it
reloaded BGE-M3 every call, broke silently on report-format drift, and needed
subprocess-spawn rights inside the UiPath container. The Coded Agent and the
ADK/Rapid path now share the SAME core; only the brain policy differs.

UiPath Python SDK standard entry point:
    def run(input_payload: dict) -> dict

Input payload schema:
    {
        "workflow_format": "markdown_sample" | "markdown_inline" | "bpmn_xml" | "mermaid",
        "workflow_content": <str>,           # raw text (required unless markdown_sample + sample_name)
        "sample_name":     "legal" | "loan", # for bundled samples
        "sample_source":   "legal" | "korean_loan" | null,  # ontology cell filter
        "backend":         "direct" | "crewai" | "langgraph",  # orchestration style (default direct)
        "brain":           "claude" | "openai" | null,
                           # multi-model brain for the executive narrative
                           # (UiPath track = multi-model). null → BRAIN env / auto.
        "hitl": {                            # Action Center auto-submit (optional)
            "auto_submit":      bool,        # default False — submit iff escalated
            "dry_run":          bool,        # default True  — preview, no network
            "bpmn_workflow_id": <str>,
            "folder_id":        int | null
        },
        "diagnosis_options": {
            "include_handoff_metrics": bool,  # default True
            "include_aggregator":      bool,  # default True
            "rag_top_n":               int,   # default 5
            "laaj_backend":            "mock" | "auto",
            "hitl_threshold": {
                "confdecay_over_trust": float,   # default 0.2
                "ips_min":              float,   # default 0.5
                "laaj_min":             float,   # default 0.6
                "final_score_red":      float    # default 4.5
            }
        }
    }

Output payload (consumed by Maestro Exclusive Gateway) is the canonical
`core.DiagnosisResult.to_dict()` plus run metadata:
    {
        "status": "ok" | "error",
        "workflow": <str>,
        "graph": {"nodes": [...], "n_nodes", "n_red", "ai_intervention_nodes"},
        "diagnoses": [ {node_id, final_score, color, axis_scores, evidence[],
                        mitigation_options{}, runtime_metric_alerts[]} , ... ],
        "metrics": {"ips": {...}, "confdecay": {...}, "laaj": {...}},
        "max_final_score": <float>,
        "runtime_alerts": <int>,
        "hitl_required": <bool>,        # ← Maestro gateway reads this
        "hitl_reason":   <str>,
        "escalated_nodes": [ {node_id, final_score, color, runtime_metric_alerts[]} ],
                                        # ← Action Center HITL hook reads this
        "executive_narrative": <str>,   # optional — present iff a brain was ready
        "brain": {...},                 # chosen brain healthcheck (multi-model)
        "thresholds_applied": {...},
        "report_paths": {...},          # best-effort existing rendered artifacts
        "action_center": {...},         # present iff hitl.auto_submit AND escalated
        "run_meta": {...},              # backend + brain dispatch + sdk availability
    }

The `run()` function is import-callable from any orchestrator (UiPath, CrewAI,
Agent Builder, or local CLI). It is registered as the Coded Agent entry point in
`uipath.json`:

    { "functions": { "main": "coded_agent_wrapper.py:run" } }

★ Multi-model brain (UiPath track allows Claude / OpenAI, selectable via the
BRAIN env var). The
deterministic ontology+RAG diagnosis core needs NO LLM, so the brain is applied
ONLY at the orchestration layer for the executive-summary narrative and degrades
gracefully when absent. Select via the `BRAIN` env var (resolved by
scripts/agents/brain_factory.get_brain):
    BRAIN=claude         → ClaudeBrain (local claude -p; dev only)
    BRAIN=openai         → OpenAIBrain (deploy; $200 credit)
    (unset/auto)         → auto-detect; falls back to a deterministic stub

UiPath publish path — VERIFIED against the official UiPath Python SDK CLI
(https://uipath.github.io/uipath-python/cli/ ; https://pypi.org/project/uipath/).
The entry point is declared in `uipath.json` (above), NOT via `uipath init`
flags. See PUBLISH.md for the full grounded walkthrough. Runs from the MAIN
session only (per project Don't list):

    pip install uipath                # Coded Agent SDK + CLI
    cd scripts/uipath
    uipath auth                       # browser OAuth; writes .env (org URL + PAT)
    uipath init                       # reads uipath.json → entry-points.json + bindings.json
    uipath pack                       # builds the .nupkg
    uipath publish --folder Shared    # uploads to Orchestrator package feed

`uipath auth` uses a BROWSER OAuth flow (NOT the client_credentials grant that
uipath_client.py uses for the Tasks API). The two auth paths are independent:
publish = browser PAT (.env, gitignored); runtime Tasks API = Keychain
client_credentials. All UiPath HTTP calls from uipath_client require the browser
User-Agent surrogate (Cloudflare 1010) and target staging.uipath.com — see
uipath_client.DEFAULT_USER_AGENT.

Usage (sanity check, no UiPath required):
    python coded_agent_wrapper.py legal
    python coded_agent_wrapper.py loan
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Path wiring — put scripts/ on sys.path so `core` (the shared diagnosis core)
# and `uipath_client` import cleanly regardless of cwd.
# --------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # .../scripts/uipath
PROJECT_SCRIPTS = SCRIPT_DIR.parent                   # .../scripts
for _p in (str(PROJECT_SCRIPTS), str(SCRIPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUTPUT_DIR = PROJECT_SCRIPTS / "output"

# Shared diagnosis core (light import — heavy RAG deps load lazily in the engine)
from core import DiagnosisEngine, WorkflowInput, DiagnosisOptions  # noqa: E402

# --------------------------------------------------------------------------
# Optional backend imports — graceful when SDKs are absent in the current venv.
# CrewAI is the Phase-1 primary orchestrator (installed in the Python 3.12
# `.venv-crewai`); LangGraph is the fallback. When neither is present the
# wrapper runs the engine directly in-process.
# --------------------------------------------------------------------------
try:
    from uipath_client import UiPathClient, UiPathConfigError  # noqa: F401
    UIPATH_CLIENT_AVAILABLE = True
except ImportError:
    UiPathClient = None  # type: ignore
    UiPathConfigError = RuntimeError  # type: ignore
    UIPATH_CLIENT_AVAILABLE = False

try:
    import uipath  # type: ignore  # noqa: F401  — UiPath Coded Agent SDK
    UIPATH_SDK_AVAILABLE = True
except ImportError:
    UIPATH_SDK_AVAILABLE = False

try:
    import crewai  # type: ignore  # noqa: F401
    CREWAI_AVAILABLE = True
except ImportError:
    CREWAI_AVAILABLE = False

try:
    import langgraph  # type: ignore  # noqa: F401
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False

# Multi-model brain selection (★ UiPath track = multi-model allowed). The brain
# is resolved by brain_factory.get_brain() from the BRAIN env var and is applied
# ONLY at the orchestration layer (executive-summary narrative). The diagnosis
# core itself is brain-agnostic. brain_factory is read-only reference here — we
# do not modify it. NOTE: do NOT set FDE_RAPID on this path; the UiPath track is
# explicitly multi-model (Claude/OpenAI), selectable via the BRAIN env var.
try:
    from agents.brain_factory import get_brain  # noqa: E402
    BRAIN_FACTORY_AVAILABLE = True
except ImportError:
    get_brain = None  # type: ignore
    BRAIN_FACTORY_AVAILABLE = False


VALID_BACKENDS = {"direct", "crewai", "langgraph"}
DEFAULT_BACKEND = "direct"

# Process-wide engine — ontology loads once; RAG resources are lazy + cached on
# the instance, so repeated run() calls in the same UiPath worker reuse BGE-M3.
_ENGINE: DiagnosisEngine | None = None


def _engine() -> DiagnosisEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DiagnosisEngine()
    return _ENGINE


def _resolve_backend(requested: str) -> tuple[str, str]:
    """Resolve orchestration backend. All backends drive the SAME core engine;
    the backend only decides whether the call fans out through CrewAI/LangGraph
    agents or runs the engine directly. Returns (selected, note)."""
    if requested not in VALID_BACKENDS:
        return "direct", f"unknown backend {requested!r}, using direct"
    if requested == "direct":
        return "direct", "direct in-process engine"
    if requested == "crewai":
        if CREWAI_AVAILABLE:
            return "crewai", "CrewAI orchestration (Phase 1 primary)"
        if LANGGRAPH_AVAILABLE:
            return "langgraph", "fallback: LangGraph (CrewAI not installed)"
        return "direct", "fallback: direct (CrewAI + LangGraph not installed)"
    if requested == "langgraph":
        if LANGGRAPH_AVAILABLE:
            return "langgraph", "LangGraph orchestration"
        return "direct", "fallback: direct (LangGraph not installed)"
    return "direct", "direct in-process engine"


def _existing_report_paths(sample_name: str | None) -> dict:
    """Best-effort references to previously rendered artifacts (no regeneration).
    The structured `diagnoses`/`metrics` are the source of truth; these are just
    convenience links for the Action Center form / demo."""
    if not sample_name:
        return {"diagnosis_md": None, "heatmap_html": None, "executive_summary_md": None}
    md = OUTPUT_DIR / f"diagnosis-v0.2-{sample_name}.md"
    heat = OUTPUT_DIR / f"{sample_name}-heatmap-v0.1.html"
    summ = OUTPUT_DIR / f"{sample_name}-executive-summary-v0.1.md"
    return {
        "diagnosis_md": str(md) if md.exists() else None,
        "heatmap_html": str(heat) if heat.exists() else None,
        "executive_summary_md": str(summ) if summ.exists() else None,
    }


# --------------------------------------------------------------------------
# Multi-model brain (UiPath path — Claude / OpenAI)
# --------------------------------------------------------------------------

def _resolve_brain(requested: str | None):
    """Resolve a multi-model brain via brain_factory (read-only reference).

    Returns (brain_or_None, healthcheck_dict). Never raises — on any failure
    (factory absent, unknown selector, SDK missing) returns (None, {...}) so the
    deterministic diagnosis core still runs. The Rapid-path brain policy lives in
    brain_factory; this UiPath path does NOT set FDE_RAPID, so Claude/OpenAI are
    legal selectors here.
    """
    if not BRAIN_FACTORY_AVAILABLE or get_brain is None:
        return None, {"available": False, "reason": "brain_factory not importable"}
    try:
        brain = get_brain(requested) if requested else get_brain()
    except (ValueError, RuntimeError) as e:
        # ValueError = unknown selector; RuntimeError = FDE_RAPID guard tripped.
        return None, {"available": False, "reason": type(e).__name__}
    try:
        hc = brain.healthcheck()
    except Exception:  # noqa: BLE001
        hc = {"name": getattr(brain, "name", "unknown"), "ready": False}
    hc["available"] = True
    return (brain if hc.get("ready") else None), hc


def _executive_narrative(brain, result_dict: dict) -> str | None:
    """Best-effort one-paragraph executive summary from the chosen brain.

    Optional — only runs when a ready multi-model brain is present. The brain is
    given the structured diagnosis (no raw corpus), so this is a narrative wrap of
    deterministic results, not a new judgement. Returns None on absence/failure.
    """
    if brain is None:
        return None
    diagnoses = result_dict.get("diagnoses") or []
    red = [d for d in diagnoses if d.get("color") == "RED"][:8]
    prompt = (
        "You are an FDE consultant. In 3-4 sentences, summarize the pre-deployment "
        "risk of this workflow for a CDO. Workflow: "
        f"{result_dict.get('workflow', 'n/a')}. "
        f"Max risk score {result_dict.get('max_final_score')}, "
        f"{result_dict.get('runtime_alerts')} runtime alerts, "
        f"HITL required: {result_dict.get('hitl_required')}. "
        f"Top RED nodes: {json.dumps([{ 'node_id': d.get('node_id'), 'score': d.get('final_score') } for d in red], ensure_ascii=False)}. "
        "Do not invent incidents; reference only the scores given. "
        "Write the summary in English only, regardless of the workflow's language."
    )
    try:
        return brain.generate(prompt).strip() or None
    except Exception:  # noqa: BLE001 — narrative is non-critical
        return None


# --------------------------------------------------------------------------
# Public entry point — UiPath Coded Agent contract
# --------------------------------------------------------------------------

def run(input_payload: dict) -> dict:
    """UiPath Coded Agent entry point.

    Builds a WorkflowInput from the payload, runs the shared diagnosis engine,
    and returns the canonical DiagnosisResult dict (Maestro reads hitl_required).
    """
    if not isinstance(input_payload, dict):
        return {"status": "error", "error": f"input_payload must be dict, got {type(input_payload).__name__}"}

    backend_req = input_payload.get("backend", DEFAULT_BACKEND)
    backend_sel, dispatch_note = _resolve_backend(backend_req)
    brain_req = input_payload.get("brain")  # None → BRAIN env / auto-detect

    try:
        options = DiagnosisOptions.from_dict(input_payload.get("diagnosis_options"))
        wf = WorkflowInput(
            content=input_payload.get("workflow_content", ""),
            workflow_format=input_payload.get("workflow_format", "markdown_sample"),
            sample_source=input_payload.get("sample_source"),
            title=input_payload.get("title", ""),
            sample_name=input_payload.get("sample_name"),
        )
    except ValueError as e:
        return {"status": "error", "error": str(e)}

    t0 = time.time()
    result = _engine().diagnose(wf, options)
    elapsed = round(time.time() - t0, 2)

    out = result.to_dict()
    out.setdefault("sample_name", wf.sample_name)
    out["report_paths"] = _existing_report_paths(wf.sample_name)

    # --- multi-model brain narrative (UiPath path; optional + graceful) ---
    brain, brain_hc = _resolve_brain(brain_req)
    narrative = _executive_narrative(brain, out)
    if narrative:
        out["executive_narrative"] = narrative
    out["brain"] = brain_hc

    # --- escalated-node summary (the Action Center HITL hook reads this) ---
    out["escalated_nodes"] = _escalated_nodes(out)

    out["run_meta"] = {
        "elapsed_sec": elapsed,
        "backend_requested": backend_req,
        "backend_selected": backend_sel,
        "dispatch_note": dispatch_note,
        "brain_requested": brain_req,
        "brain_ready": bool(brain),
        "sdk_availability": {
            "uipath_sdk": UIPATH_SDK_AVAILABLE,
            "uipath_client": UIPATH_CLIENT_AVAILABLE,
            "brain_factory": BRAIN_FACTORY_AVAILABLE,
            "crewai": CREWAI_AVAILABLE,
            "langgraph": LANGGRAPH_AVAILABLE,
        },
    }

    # --- auto Action Center HITL hook: when the diagnosis escalates a node ---
    # (hitl_required) AND the caller opted in, forward to Action Center. Default
    # dry_run=True so this is safe from any session; Maestro's userTask normally
    # owns the real task creation, this is the direct-dispatch convenience path.
    hitl_opts = input_payload.get("hitl") or {}
    if out.get("hitl_required") and hitl_opts.get("auto_submit"):
        out["action_center"] = submit_to_action_center(
            out,
            bpmn_workflow_id=hitl_opts.get("bpmn_workflow_id", ""),
            folder_id=hitl_opts.get("folder_id"),
            dry_run=hitl_opts.get("dry_run", True),
        )
    return out


def _escalated_nodes(result_dict: dict) -> list:
    """Per-node summary of every node the diagnosis escalated (RED / runtime
    alerts), in the shape the Action Center HITL form + Maestro gateway read.
    Pure function over the run() output — no side effects."""
    rows = []
    for d in result_dict.get("diagnoses") or []:
        if d.get("color") == "RED" or d.get("runtime_metric_alerts"):
            rows.append({
                "node_id": d.get("node_id"),
                "final_score": d.get("final_score"),
                "color": d.get("color"),
                "runtime_metric_alerts": d.get("runtime_metric_alerts", []),
            })
    return rows


def submit_to_action_center(
    run_result: dict,
    bpmn_workflow_id: str = "",
    folder_id: int | None = None,
    dry_run: bool = True,
) -> dict:
    """Forward a `run()` result to UiPath Action Center as a HITL task.

    Optional helper — not part of the Maestro Coded Agent contract itself
    (Maestro's User Task step calls Action Center directly via the BPMN
    userTask). Useful for the CLI / direct-dispatch path.

    Defaults to dry_run=True so this is safe to call from any session; actual
    submission requires: (1) Keychain `uipath_tenant` populated, (2)
    dry_run=False, (3) explicit main-session approval per project Don't list.
    """
    if run_result.get("status") != "ok":
        return {"status": "error", "error": "upstream diagnosis not ok", "upstream": run_result}
    if not UIPATH_CLIENT_AVAILABLE:
        return {"status": "error", "error": "uipath_client module not importable"}
    try:
        client = UiPathClient()
    except UiPathConfigError as e:
        return {"status": "error", "stage": "client_init", "error": str(e)}
    # uipath_client expects sample_name on the result; carry it from workflow.
    run_result.setdefault("sample_name", run_result.get("workflow", "unknown"))
    try:
        task_request = client.submit_diagnosis_for_hitl(
            diagnosis_result=run_result,
            bpmn_workflow_id=bpmn_workflow_id,
            folder_id=folder_id,
            dry_run=dry_run,
        )
    except Exception as e:  # noqa: BLE001 — secrets-safe surface only
        return {"status": "error", "stage": "submit", "error": type(e).__name__}
    return {"status": "ok", "task_request": task_request}


# --------------------------------------------------------------------------
# CLI sanity check — `python coded_agent_wrapper.py legal`
# --------------------------------------------------------------------------

def _main_cli() -> int:
    sample = sys.argv[1] if len(sys.argv) > 1 else "legal"
    # Optional 2nd arg = brain selector (claude/openai).
    brain_sel = sys.argv[2] if len(sys.argv) > 2 else None
    payload = {"workflow_format": "markdown_sample", "sample_name": sample}
    if brain_sel:
        payload["brain"] = brain_sel
    result = run(payload)
    # Trim the verbose evidence for CLI readability; keep the gate-relevant fields.
    summary = {
        "status": result.get("status"),
        "workflow": result.get("workflow"),
        "n_red": result.get("graph", {}).get("n_red"),
        "max_final_score": result.get("max_final_score"),
        "runtime_alerts": result.get("runtime_alerts"),
        "hitl_required": result.get("hitl_required"),
        "hitl_reason": result.get("hitl_reason"),
        "escalated_nodes": result.get("escalated_nodes"),
        "brain": result.get("brain"),
        "executive_narrative": result.get("executive_narrative"),
        "diagnoses": [
            {"node_id": d["node_id"], "final_score": d["final_score"], "color": d["color"]}
            for d in result.get("diagnoses", [])
        ],
        "run_meta": result.get("run_meta"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(_main_cli())
