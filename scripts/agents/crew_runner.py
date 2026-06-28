"""
FDE Agent — Real CrewAI Wiring (UiPath path, multi-model)
=========================================================

Turns the crew_poc.py skeleton (ROLE_SPECS + TASK_GRAPH) into a runnable CrewAI
Crew whose 5 agents call the SHARED diagnosis core (`core.tools`) through real
CrewAI tools. This is the UiPath-path orchestrator (multi-model allowed); the
Rapid path uses ADK over the same core tools.

★ Runs in the Python 3.12 `scripts/.venv-crewai` venv — CrewAI requires
`>=3.10,<3.14` and cannot install in the main 3.14 venv. All `crewai` imports
are inside functions so this module still imports cleanly under 3.14 (for lint /
the crew_poc.py delegation probe).

Brain policy: the LLM is built from env (CREW_MODEL, default OpenAI). For
the UiPath path you may set CREW_MODEL to a Claude or OpenAI
model — multi-model is allowed here. The Rapid path must NOT use this module
with any litellm-supported model.

Tools (each wraps a pure core.tools function — same I/O contract as the ADK path):
    parse_workflow      → core.tools.parse_workflow
    ontology_lookup     → core.tools.ontology_lookup        (cells from engine)
    retrieve_incidents  → core.tools.retrieve_incidents      (RAG via engine; degrades)
    score_workflow      → engine.diagnose (aggregate + 3-axis + HITL gate)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from agents.crew_poc import ROLE_SPECS, TASK_GRAPH  # noqa: E402  (spec, no crewai dep)


# --------------------------------------------------------------------------
# Shared engine singleton — tools reuse one ontology load + lazy RAG cache.
# --------------------------------------------------------------------------
_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from core import DiagnosisEngine
        _ENGINE = DiagnosisEngine()
    return _ENGINE


# --------------------------------------------------------------------------
# CrewAI tool definitions (imported lazily so module loads under py3.14 too).
# --------------------------------------------------------------------------

def build_tools() -> dict:
    """Build the CrewAI tool objects that wrap core.tools. Returns {name: tool}."""
    from crewai.tools import tool  # py3.12 venv only

    @tool("parse_workflow")
    def parse_workflow_tool(workflow_markdown: str) -> str:
        """Parse a Layer-2 workflow markdown ('## Node Inventory' table) into a
        JSON list of nodes [{id, function, ai_mode, predicted_color}]."""
        from core import tools as T
        return json.dumps(T.parse_workflow(workflow_markdown))

    @tool("ontology_lookup")
    def ontology_lookup_tool(node_id: str, sample_source: str = "") -> str:
        """Look up the mapping-ontology risk cells for one node id, grouped by the
        3 axes (general_failure / security / handoff). sample_source filters cells
        ('legal' | 'korean_loan' | '')."""
        from core import tools as T
        by_axis = T.ontology_lookup(node_id, _engine().cells, sample_source or None)
        return json.dumps(by_axis, default=str)

    @tool("retrieve_incidents")
    def retrieve_incidents_tool(query: str) -> str:
        """Retrieve up to 5 similar AIID incidents for a risk query via BGE-M3 +
        Chroma RAG. Returns JSON; degrades to [] when the corpus is unavailable."""
        from core import tools as T
        eng = _engine()
        if not eng._ensure_rag():
            return json.dumps({"degraded": True, "incidents": []})
        return json.dumps(T.retrieve_incidents(query, eng.embed_fn, eng._collection, n=5))

    @tool("score_workflow")
    def score_workflow_tool(workflow_markdown: str, sample_source: str = "") -> str:
        """Run the full 3-axis aggregation + handoff metrics + HITL gate on a
        workflow markdown. Returns the canonical DiagnosisResult JSON."""
        from core import WorkflowInput, DiagnosisOptions
        wf = WorkflowInput(
            content=workflow_markdown,
            workflow_format="markdown_inline",
            sample_source=sample_source or None,
        )
        res = _engine().diagnose(wf, DiagnosisOptions())
        return json.dumps(res.to_dict(), default=str)

    return {
        "parse_bpmn_md": parse_workflow_tool,
        "ontology_lookup": ontology_lookup_tool,
        "chroma_search": retrieve_incidents_tool,
        "aggregator": score_workflow_tool,
    }


def build_llm():
    """CrewAI LLM via litellm. CREW_MODEL selects the provider/model.

    UiPath path (multi-model): e.g.
        gpt-4o-mini                      (OpenAI, default)
        gpt-4o                           (OpenAI)
    """
    from crewai import LLM
    model = os.environ.get("CREW_MODEL", "gpt-4o-mini")
    return LLM(model=model)


# Map ROLE_SPECS tool keys → the subset we actually wired. Roles whose tools are
# not yet wired (owasp/mitre/mit_taxonomy live inside ontology cells) fall back
# to the ontology_lookup tool, which already carries standards + mitigation data.
_TOOL_ALIAS = {
    "parse_bpmn_md": "parse_bpmn_md",
    "ontology_lookup": "ontology_lookup",
    "ips": "ontology_lookup", "confdecay": "ontology_lookup", "laaj": "ontology_lookup",
    "owasp_lookup": "ontology_lookup", "mitre_lookup": "ontology_lookup",
    "chroma_search": "chroma_search",
    "mit_taxonomy": "ontology_lookup",
}


def build_crew(llm=None):
    """Assemble the real CrewAI Crew from ROLE_SPECS + TASK_GRAPH.

    Returns the Crew object. Does NOT kick off — call .kickoff(inputs=...) with a
    live LLM (provider creds present). Building is creds-free (smoke test)."""
    from crewai import Agent, Task, Crew, Process

    llm = llm or build_llm()
    tools = build_tools()

    agents = {}
    for spec in ROLE_SPECS:
        agent_tools = []
        seen = set()
        for tkey in spec["tools"]:
            wired = _TOOL_ALIAS.get(tkey)
            if wired and wired in tools and wired not in seen:
                agent_tools.append(tools[wired])
                seen.add(wired)
        # The Mitigation Recommender is the final synthesis step (depends on all
        # upstream tasks) — give it the score_workflow/aggregator tool so it can
        # emit the aggregated final scores + HITL gate.
        if spec["role"] == "Mitigation Recommender" and "aggregator" not in seen:
            agent_tools.append(tools["aggregator"])
            seen.add("aggregator")
        agents[spec["role"]] = Agent(
            role=spec["role"],
            goal=spec["goal"],
            backstory=spec["backstory"],
            tools=agent_tools,
            llm=llm,
            verbose=False,
            allow_delegation=False,
        )

    role_task_desc = {
        "BPMN Parser": "Parse the provided workflow markdown into a node graph using the parse_workflow tool. List every node id with its function and predicted risk color.",
        "Risk Diagnoser": "For each RED node, call ontology_lookup to obtain the 3-axis risk cells. Summarize the dominant failure mode per axis.",
        "Standards Mapper": "Using the ontology cells, extract the OWASP LLM Top 10 and MITRE ATLAS mappings already present per node.",
        "AIID Retriever": "For each RED node, call retrieve_incidents with a risk query to fetch similar real-world AI incidents.",
        "Mitigation Recommender": "Call score_workflow to produce the aggregated final scores and HITL gate, then recommend Must-Fix / Recommend / Optional mitigations per RED node.",
    }

    tasks = []
    for t in TASK_GRAPH:
        role = t["agent_role"]
        tasks.append(Task(
            description=role_task_desc.get(role, f"Execute {t['task']}."),
            expected_output="A concise structured summary for the FDE Agent diagnosis report.",
            agent=agents[role],
            async_execution=t["async"],
        ))

    return Crew(agents=list(agents.values()), tasks=tasks, process=Process.sequential, verbose=False)


def smoke_test() -> dict:
    """Verify the crew BUILDS (agents/tasks/tools instantiate) without requiring
    live LLM creds. Returns a summary dict."""
    from crewai import Agent  # noqa: F401  — assert crewai importable
    tools = build_tools()
    crew = build_crew(llm=build_llm())
    return {
        "crewai_importable": True,
        "tools_wired": sorted(tools.keys()),
        "n_agents": len(crew.agents),
        "n_tasks": len(crew.tasks),
        "agent_roles": [a.role for a in crew.agents],
        "tools_per_agent": {a.role: [getattr(t, "name", str(t)) for t in a.tools] for a in crew.agents},
    }


def kickoff(workflow_markdown: str, sample_source: str = "") -> Any:
    """Run the crew end-to-end. Requires a live LLM (provider creds)."""
    crew = build_crew()
    return crew.kickoff(inputs={"workflow_markdown": workflow_markdown, "sample_source": sample_source})


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="FDE Agent — real CrewAI crew")
    ap.add_argument("--smoke", action="store_true", help="build-only smoke test (no LLM call)")
    ap.add_argument("--sample", default="legal", help="bundled sample for kickoff (legal|loan)")
    args = ap.parse_args()

    if args.smoke:
        print(json.dumps(smoke_test(), indent=2, ensure_ascii=False))
    else:
        from core import SAMPLE_FILES, SAMPLE_SOURCE
        md = SAMPLE_FILES[args.sample].read_text(encoding="utf-8")
        out = kickoff(md, SAMPLE_SOURCE[args.sample])
        print(out)
