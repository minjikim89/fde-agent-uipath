"""
CrewAI 5-Role Wrap PoC (applying Blocker #5 conclusions).

Decisions from Blocker #5 report (`_research/2026-05-25-crewai-phoenix-trace.md`):
  - Process.sequential primary, implicit parallel via async_execution=True
  - 5 fixed roles: BPMN Parser / Risk Diagnoser / Standards Mapper / AIID Retriever / Mitigation Recommender
  - (optional) a peer-review pass — a possible future extension
  - Phoenix instrumentation: single line `phoenix.otel.register(auto_instrument=True)`
  - This PoC is an environment-agnostic skeleton — gracefully skips crewai/phoenix if not installed

Sanity check purposes before entering GCP Agent Builder:
  - Syntactic correctness of 5-role definitions — can be copied as-is for Phase 1 build
  - Phoenix instrumentation setup pattern baked in
  - tool function signature freeze (parser/risk/standards/rag/mitigation)
  - All sub-agent input/output schemas specified → consumed by Aggregator

Run modes:
  python3 crew_poc.py                # smoke test (stub LLM, deterministic)
  python3 crew_poc.py --check-deps   # crewai/phoenix import availability check only
"""
from __future__ import annotations
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# PoC assets — wrap as tools for real LLM-backed agents on Phase 1 entry
TOOLS_NEEDED = {
    "parse_bpmn_md":  "scripts/diagnose.py § parse_workflow",
    "ontology_lookup":"scripts/diagnose.py § cells_for_node",
    "owasp_lookup":   "scripts/data/owasp-llm-top10-v2025.yaml",
    "mitre_lookup":   "scripts/data/mitre-atlas.yaml",
    "chroma_search":  "scripts/diagnose.py § retrieve_aiid (BGE-M3 + Chroma)",
    "mit_taxonomy":   "scripts/data/mit-mitigation-taxonomy.yaml",
    "ips":            "scripts/metrics/ips.py",
    "confdecay":      "scripts/metrics/confdecay.py (4-source)",
    "laaj":           "scripts/metrics/laaj.py",
    "aggregator":     "scripts/agents/aggregator.py",
}


# =============================================================================
# 1) Role definitions — use as-is for Phase 1 build
# =============================================================================

ROLE_SPECS = [
    {
        "role": "BPMN Parser",
        "goal": "Extract a topological graph from a workflow diagram (BPMN XML / Mermaid / image). "
                "Classify each node's type / ai_mode / dependency relationships.",
        "backstory": "A process engineer who has normalized hundreds of enterprise workflows. "
                     "Tracks handoffs not explicitly stated in diagrams, documents, or SOPs.",
        "tools":     ["parse_bpmn_md"],
        "llm_role":  "primary_llm",     # Rapid Agent: primary LLM
        "outputs":   ["nodes: list[Node]", "edges: list[Edge]"],
    },
    {
        "role": "Risk Diagnoser",
        "goal": "Diagnose each node's 8-axis risk vector + cell-level diagnosis based on the Mapping ontology (★ proprietary IP) ruleset.",
        "backstory": "A risk analyst who has analyzed 1,400+ AI system incident cases. "
                     "Identifies even handoff and exception-handling heuristic areas lacking public data.",
        "tools":     ["ontology_lookup", "ips", "confdecay", "laaj"],
        "llm_role":  "primary_llm",
        "outputs":   ["per-node cells_by_axis", "handoff_metrics (IPS/CD/LaaJ)"],
    },
    {
        "role": "Standards Mapper",
        "goal": "Map each node's risk to OWASP LLM Top 10 v2025 + MITRE ATLAS v5.6.0.",
        "backstory": "A security standards curator. Automatically maps items like LLM06 Excessive Agency and AML.T0043.",
        "tools":     ["owasp_lookup", "mitre_lookup"],
        "llm_role":  "primary_llm",
        "outputs":   ["per-node standards mapping (LLM01~LLM10, AML.T*, NIST AI RMF)"],
    },
    {
        "role": "AIID Retriever",
        "goal": "Retrieve 3–5 similar incidents from the AIID/AIAAIC corpus per high-risk node.",
        "backstory": "Searches 7,959 incident vectors (1,480 + 6,479 reports) via BGE-M3. "
                     "Automatically cites Air Canada / Klarna-style references.",
        "tools":     ["chroma_search"],
        "llm_role":  "primary_llm",
        "outputs":   ["per high-risk node 3~5 incidents w/ similarity + title + date"],
    },
    {
        "role": "Mitigation Recommender",
        "goal": "Propose per-high-risk node multi-option (Must Fix / Recommend / Optional) playbooks. "
                "Based on MIT Mitigation Taxonomy (831) + OWASP prevention + proprietary IP ruleset.",
        "backstory": "McKinsey-style multi-scenario consulting tone — no single fix pushed, options + trade-offs.",
        "tools":     ["mit_taxonomy"],
        "llm_role":  "primary_llm",
        "outputs":   ["per-node mitigation_options {must_fix, recommend, optional} + trade-off matrix"],
    },
]


# =============================================================================
# 2) Task graph — sequential w/ async parallel
# =============================================================================

TASK_GRAPH = [
    {"task": "parse_workflow",    "agent_role": "BPMN Parser",            "depends_on": [],                                                   "async": False},
    {"task": "diagnose_risk",     "agent_role": "Risk Diagnoser",         "depends_on": ["parse_workflow"],                                   "async": True},
    {"task": "map_standards",     "agent_role": "Standards Mapper",       "depends_on": ["parse_workflow"],                                   "async": True},
    {"task": "retrieve_incidents","agent_role": "AIID Retriever",         "depends_on": ["diagnose_risk"],                                    "async": True},
    {"task": "recommend_mitig",   "agent_role": "Mitigation Recommender", "depends_on": ["diagnose_risk", "map_standards", "retrieve_incidents"], "async": False},
]


# =============================================================================
# 3) Phoenix instrumentation setup spec (apply on Phase 1 entry)
# =============================================================================

PHOENIX_SETUP_SPEC = """\
# One-line setup for Phase 1 entry (Blocker #3 conclusion):
from phoenix.otel import register
tracer_provider = register(
    project_name="fde-agent-rapid-agent-demo",
    auto_instrument=True,
)
# auto_instrument=True → automatically enables installed OpenInference instrumentors:
#   - openinference-instrumentation-crewai
#   - openinference-instrumentation-litellm   (via LiteLLM in CrewAI ≥0.63)
#   - (optional tracing instrumentation)
#   - openinference-instrumentation-anthropic (optional tracing)
"""


# =============================================================================
# 4) Stub Crew (environment-agnostic) — gracefully handles missing crewai install
# =============================================================================

@dataclass
class StubAgent:
    role: str
    goal: str
    backstory: str
    tools: list = field(default_factory=list)
    llm_role: str = "primary_llm"

    def execute(self, task_name: str, inputs: dict) -> dict:
        # Stub LLM: echo input as-is + role tag
        return {
            "agent_role": self.role,
            "task": task_name,
            "inputs_keys": list(inputs.keys()),
            "output_marker": f"[{self.role}] would produce: {task_name}",
            "llm_role": self.llm_role,
            "tools_used": self.tools,
        }


@dataclass
class StubTask:
    name: str
    agent_role: str
    depends_on: list
    async_execution: bool


def build_stub_crew() -> tuple[list, list]:
    """Environment-agnostic build — sanity check that 5-role definitions survive without crewai."""
    agents = [StubAgent(role=s["role"], goal=s["goal"], backstory=s["backstory"],
                        tools=s["tools"], llm_role=s["llm_role"])
              for s in ROLE_SPECS]
    tasks = [StubTask(name=t["task"], agent_role=t["agent_role"],
                      depends_on=t["depends_on"], async_execution=t["async"])
             for t in TASK_GRAPH]
    return agents, tasks


def stub_run(agents: list, tasks: list, bpmn_path: str = "scripts/data/sample-workflows/legal-contract-review-v0.1.md") -> list:
    """Stub orchestrator — replace with CrewAI Crew.kickoff() on Phase 1 entry."""
    agents_by_role = {a.role: a for a in agents}
    completed = {}
    trace = []
    for t in tasks:
        agent = agents_by_role[t.agent_role]
        inputs = {"bpmn_path": bpmn_path} if t.name == "parse_workflow" else {dep: completed[dep] for dep in t.depends_on}
        trace_entry = {"task": t.name, "agent": agent.role, "async": t.async_execution,
                       "depends_on": t.depends_on, "ts": time.time()}
        result = agent.execute(t.name, inputs)
        trace_entry["output_marker"] = result["output_marker"]
        completed[t.name] = result
        trace.append(trace_entry)
    return trace


# =============================================================================
# 5) Real CrewAI path (activate after Phase 1 entry)
# =============================================================================

def real_crew_build():
    """
    Activate on Phase 1 entry. This function is a placeholder that only attempts imports — returns None if not installed.
    See function body docstring + _research/2026-05-25-crewai-phoenix-trace.md §2 for actual build code pattern.
    """
    try:
        from crewai import Agent, Task, Crew, Process
        try:
            import litellm  # noqa: F401  # provider-agnostic LLM router
            llm_ready = True
        except ImportError:
            llm_ready = False

        try:
            from phoenix.otel import register
            register(project_name="fde-agent-rapid-agent-demo", auto_instrument=True)
            phoenix_status = "registered"
        except ImportError:
            phoenix_status = "not_installed"

        # Actual build happens after Phase 1 entry — this function is a prerequisites checker
        return {"crewai": True, "llm": llm_ready, "phoenix": phoenix_status}
    except ImportError as e:
        return {"crewai": False, "missing": str(e)}


# =============================================================================
# 6) entrypoint
# =============================================================================

def check_dependencies() -> dict:
    status = {}
    for mod in ["crewai", "phoenix", "openinference.instrumentation.crewai",
                "anthropic"]:
        try:
            __import__(mod)
            status[mod] = "installed"
        except ImportError:
            status[mod] = "NOT_installed"
    return status


def main():
    if "--check-deps" in sys.argv:
        print("--- Phase 1 dependency status ---")
        for mod, st in check_dependencies().items():
            mark = "✅" if st == "installed" else "⏳"
            print(f"  {mark} {mod}: {st}")
        return

    print("=== CrewAI 5-Role Wrap PoC (skeleton sanity) ===\n")
    print(f"[1/4] Role specs: {len(ROLE_SPECS)} agents")
    for s in ROLE_SPECS:
        tools_str = ', '.join(s['tools'])
        print(f"  - {s['role']:30s} llm={s['llm_role']:18s} tools=[{tools_str}]")

    print(f"\n[2/4] Task graph: {len(TASK_GRAPH)} tasks (sequential w/ async parallel)")
    for t in TASK_GRAPH:
        async_tag = "[async]" if t['async'] else "       "
        deps = ', '.join(t['depends_on']) or '—'
        print(f"  {async_tag} {t['task']:22s} agent={t['agent_role']:30s} depends_on=[{deps}]")

    print(f"\n[3/4] Phoenix setup spec:")
    for line in PHOENIX_SETUP_SPEC.split('\n'):
        if line.strip():
            print(f"    {line}")

    print(f"\n[4/4] Stub orchestrator e2e (environment-agnostic):")
    agents, tasks = build_stub_crew()
    trace = stub_run(agents, tasks)
    for entry in trace:
        async_tag = "[async]" if entry['async'] else "       "
        print(f"  {async_tag} {entry['task']:22s} → {entry['output_marker'][:80]}")

    print(f"\nReal CrewAI path probe:")
    probe = real_crew_build()
    for k, v in probe.items():
        print(f"  {k}: {v}")

    # sanity invariants
    assert len(ROLE_SPECS) == 5, "5 role definitions are broken"
    assert len(TASK_GRAPH) == len(ROLE_SPECS), "task ↔ agent 1:1 mapping is broken"
    assert all(t['agent_role'] in {s['role'] for s in ROLE_SPECS} for t in TASK_GRAPH), "task agent_role missing"
    print("\n✅ crew_poc.py sanity invariants passed (5 roles · 5 tasks · trace e2e)")


if __name__ == "__main__":
    main()
