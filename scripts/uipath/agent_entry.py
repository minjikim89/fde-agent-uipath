"""
UiPath Coded AGENT entry point for FDE Agent — Track 2 (Maestro BPMN)
====================================================================

This is the AGENT-typed entry point (ProjectType "Agent"), distinct from the
deterministic Coded FUNCTION in `coded_agent_wrapper.py:run`. UiPath's model:

    Coded Function = deterministic Python, no LLM   (our diagnosis CORE)
    Coded Agent    = LLM reasoning loop + framework  (this wrapper)

Maestro's Service Task "Start and wait for agent" only binds AGENTS, so we
expose the diagnosis as an agent: the deterministic ontology+RAG engine runs the
diagnosis, and the multi-model brain (Claude / OpenAI, resolved by
brain_factory) writes the executive narrative — that LLM step is the agent's
reasoning surface. The deterministic core is reused verbatim via `run()`; this
file only adapts it to the typed dataclass Input/Output the agent runtime wants.

Contract (UiPath Python SDK coded-agent):
    uipath.json   -> { "agents": { "agent": "agent_entry.py:agent" } }
    pyproject     -> [tool.uipath] type = "agent"
    entry point   -> def agent(input: Input) -> Output   (dataclasses, no decorator)

Output fields are flat scalars so Maestro can bind them directly to gateway
conditions and Action Center inputs (esp. `hitl_required`, `max_final_score`).
`escalated_json` carries the per-node RED/alert detail as a JSON string for the
Action Center HITL form.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

# Reuse the verified deterministic core + multi-model brain wrapper.
from coded_agent_wrapper import run


@dataclass
class Input:
    # Bundled sample ("legal" | "loan") OR raw workflow text.
    sample_name: Optional[str] = None
    workflow_content: str = ""
    workflow_format: str = "markdown_sample"   # markdown_sample|markdown_inline|bpmn_xml|mermaid
    sample_source: Optional[str] = None        # "legal" | "korean_loan"
    brain: Optional[str] = None                # claude|openai (null -> BRAIN env)


@dataclass
class Output:
    status: str = "error"
    workflow: str = ""
    n_nodes: int = 0
    n_red: int = 0
    max_final_score: float = 0.0
    runtime_alerts: int = 0
    hitl_required: bool = False                # ← Maestro Exclusive Gateway reads this
    hitl_reason: str = ""
    executive_narrative: str = ""              # ← LLM (agent reasoning) output, may be empty if no brain
    escalated_json: str = "[]"                 # ← per-node RED/alert detail for Action Center HITL form
    brain_name: str = ""


def agent(input: Input) -> Output:
    """UiPath coded-agent entry point. Runs the deterministic diagnosis engine
    and the multi-model brain narrative, returning a flat typed result Maestro
    can bind to gateways and Action Center inputs."""
    payload = {
        "workflow_format": input.workflow_format,
        "sample_name": input.sample_name,
        "workflow_content": input.workflow_content,
        "sample_source": input.sample_source,
        "brain": input.brain,
    }
    r = run(payload)
    graph = r.get("graph", {}) or {}
    brain = r.get("brain", {}) or {}
    return Output(
        status=r.get("status", "error"),
        workflow=r.get("workflow", "") or "",
        n_nodes=int(graph.get("n_nodes", 0) or 0),
        n_red=int(graph.get("n_red", 0) or 0),
        max_final_score=float(r.get("max_final_score", 0.0) or 0.0),
        runtime_alerts=int(r.get("runtime_alerts", 0) or 0),
        hitl_required=bool(r.get("hitl_required", False)),
        hitl_reason=r.get("hitl_reason", "") or "",
        executive_narrative=(r.get("executive_narrative") or ""),
        escalated_json=json.dumps(r.get("escalated_nodes", []), ensure_ascii=False, default=str),
        brain_name=str(brain.get("name") or brain.get("reason") or ""),
    )
