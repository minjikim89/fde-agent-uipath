"""
CrewAI 5-Role Wrap PoC (블로커 #5 결론 적용).

블로커 #5 보고서 (`_research/2026-05-25-crewai-phoenix-trace.md`)의 결정 그대로:
  - Process.sequential primary, async_execution=True로 implicit parallel
  - 5 fixed role: BPMN Parser / Risk Diagnoser / Standards Mapper / AIID Retriever / Mitigation Recommender
  - (선택) Sub-Agent 6 Peer Reviewer (Claude) — Phase 1 진입 후 통합
  - Phoenix instrumentation: `phoenix.otel.register(auto_instrument=True)` 한 줄
  - 본 PoC는 environment-agnostic skeleton — crewai/phoenix 미설치 시 graceful 단축

GCP Agent Builder 진입 전 sanity check 용도:
  - 5-role 정의 syntactic 정확성 — Phase 1 빌드 시 그대로 복사 가능
  - Phoenix instrumentation 셋업 패턴 박음
  - tool function signature freeze (parser/risk/standards/rag/mitigation)
  - 모든 sub-agent의 input/output schema 명시 → Aggregator가 consume

Run modes:
  python3 crew_poc.py                # smoke test (stub LLM, deterministic)
  python3 crew_poc.py --check-deps   # crewai/phoenix import 가능 여부만 보고
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

# 본 PoC 자산 — Phase 1 진입 시 실제 LLM-backed agent의 tool로 wrap
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
# 1) Role definitions — Phase 1 빌드 시 그대로 사용
# =============================================================================

ROLE_SPECS = [
    {
        "role": "BPMN Parser",
        "goal": "워크플로우 도식(BPMN XML / Mermaid / 이미지)을 topological graph로 추출. "
                "각 노드의 type / ai_mode / 의존 관계를 분류.",
        "backstory": "수백 건의 enterprise workflow를 정규화해 본 process engineer. "
                     "도식·문서·SOP에서 명시 안 된 handoff까지 추적.",
        "tools":     ["parse_bpmn_md"],
        "llm_role":  "primary_llm",     # Rapid Agent: primary LLM
        "outputs":   ["nodes: list[Node]", "edges: list[Edge]"],
    },
    {
        "role": "Risk Diagnoser",
        "goal": "각 노드의 8-axis risk vector + cell-level 진단. Mapping ontology(★ 본인 IP) 룰셋 기반.",
        "backstory": "AI 시스템 사고 사례 1,400+건을 분석한 risk analyst. "
                     "공개 데이터 없는 handoff·예외처리 휴리스틱 영역까지 식별.",
        "tools":     ["ontology_lookup", "ips", "confdecay", "laaj"],
        "llm_role":  "primary_llm",
        "outputs":   ["per-node cells_by_axis", "handoff_metrics (IPS/CD/LaaJ)"],
    },
    {
        "role": "Standards Mapper",
        "goal": "각 노드 risk를 OWASP LLM Top 10 v2025 + MITRE ATLAS v5.6.0 매핑.",
        "backstory": "보안 표준 큐레이터. LLM06 Excessive Agency와 AML.T0043 같은 매핑이 자동.",
        "tools":     ["owasp_lookup", "mitre_lookup"],
        "llm_role":  "primary_llm",
        "outputs":   ["per-node standards mapping (LLM01~LLM10, AML.T*, NIST AI RMF)"],
    },
    {
        "role": "AIID Retriever",
        "goal": "high-risk 노드별 AIID/AIAAIC corpus에서 유사 사고 3~5건 retrieval.",
        "backstory": "incident 7,959 vectors(1,480 + 6,479 reports)를 BGE-M3로 검색. "
                     "Air Canada / Klarna 류 레퍼런스를 자동 인용.",
        "tools":     ["chroma_search"],
        "llm_role":  "primary_llm",
        "outputs":   ["per high-risk node 3~5 incidents w/ similarity + title + date"],
    },
    {
        "role": "Mitigation Recommender",
        "goal": "high-risk 노드별 multi-option (Must Fix / Recommend / Optional) playbook 제안. "
                "MIT Mitigation Taxonomy(831) + OWASP prevention + 본인 IP 룰셋.",
        "backstory": "McKinsey 식 multi-scenario consulting 톤 — 단일 fix 강요 X, 옵션 + trade-off.",
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
# 3) Phoenix instrumentation setup spec (Phase 1 진입 시 적용)
# =============================================================================

PHOENIX_SETUP_SPEC = """\
# Phase 1 진입 시 셋업 한 줄 (블로커 #3 결론):
from phoenix.otel import register
tracer_provider = register(
    project_name="fde-agent-rapid-agent-demo",
    auto_instrument=True,
)
# auto_instrument=True → 설치된 OpenInference instrumentor 자동 enable:
#   - openinference-instrumentation-crewai
#   - openinference-instrumentation-litellm   (CrewAI ≥0.63 LiteLLM 경유)
#   - (optional tracing instrumentation)
#   - openinference-instrumentation-anthropic (Sub-Agent 6 Claude peer reviewer)
"""


# =============================================================================
# 4) Stub Crew (environment-agnostic) — crewai 미설치 시도 graceful
# =============================================================================

@dataclass
class StubAgent:
    role: str
    goal: str
    backstory: str
    tools: list = field(default_factory=list)
    llm_role: str = "primary_llm"

    def execute(self, task_name: str, inputs: dict) -> dict:
        # Stub LLM: 입력을 그대로 echo + role tag
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
    """environment-agnostic 빌드 — crewai 부재해도 5-role 정의가 살아있는지 sanity."""
    agents = [StubAgent(role=s["role"], goal=s["goal"], backstory=s["backstory"],
                        tools=s["tools"], llm_role=s["llm_role"])
              for s in ROLE_SPECS]
    tasks = [StubTask(name=t["task"], agent_role=t["agent_role"],
                      depends_on=t["depends_on"], async_execution=t["async"])
             for t in TASK_GRAPH]
    return agents, tasks


def stub_run(agents: list, tasks: list, bpmn_path: str = "scripts/data/sample-workflows/legal-contract-review-v0.1.md") -> list:
    """Stub orchestrator — Phase 1 진입 시 CrewAI Crew.kickoff()로 교체."""
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
# 5) Real CrewAI path (Phase 1 진입 후 활성화)
# =============================================================================

def real_crew_build():
    """
    Phase 1 진입 시 활성화. 본 함수는 placeholder로 import 시도만 — 미설치 시 None 반환.
    실제 빌드 코드 패턴은 함수 본문 docstring + _research/2026-05-25-crewai-phoenix-trace.md §2 참조.
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

        # 실제 빌드는 Phase 1 진입 후 — 본 함수는 prerequisites checker
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
    assert len(ROLE_SPECS) == 5, "5 role 정의가 깨졌음"
    assert len(TASK_GRAPH) == len(ROLE_SPECS), "task ↔ agent 1:1 매핑 깨졌음"
    assert all(t['agent_role'] in {s['role'] for s in ROLE_SPECS} for t in TASK_GRAPH), "task agent_role 누락"
    print("\n✅ crew_poc.py sanity invariants passed (5 roles · 5 tasks · trace e2e)")


if __name__ == "__main__":
    main()
