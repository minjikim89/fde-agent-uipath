"""
FDE Agent — Executive Summary Generator (Layer 3 Render)

입력: TopologicalGraph + per-node NodeMitigationDossier list + ontology (cumulative_scenarios 활용)
출력: Markdown 1 page (~30~50 lines), CDO/CIO 친화 layout
산출 파일: scripts/output/{sample}-executive-summary-v0.1.md

섹션 (architecture.md §8 RENDER의 'executive summary' 컴포넌트):
  a. 전체 위험 등급 (CRITICAL / HIGH / MEDIUM / LOW)
  b. RED 노드 N개 list + 핵심 failure mode 1줄
  c. 핵심 failure mode top-3 (frequency + impact 기준)
  d. Cumulative Scenario 권고 (Minimum / Balanced / Maximum Safety) — ontology field 그대로 + cell 근거
  e. 추정 ROI 한 줄
  f. 다음 액션 (3-step playbook 첫 단계)

Devpost 영상 마지막 30초 hero shot 가독성 우선.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..parser import TopologicalGraph
from ..agents import NodeMitigationDossier, SubAgent5MitigationRecommender


# =============================================================
# Risk grading
# =============================================================

def grade_workflow(graph: TopologicalGraph, dossiers: list[NodeMitigationDossier]) -> tuple[str, str]:
    """전체 워크플로우 위험 등급 + 한국어 한 줄 설명 return."""
    red_count = sum(1 for n in graph.nodes if n.color == "RED")
    yellow_count = sum(1 for n in graph.nodes if n.color == "YELLOW")
    red_dossiers = [d for d in dossiers if d.color == "RED"]
    avg_red_risk = (
        sum(d.aggregate_risk for d in red_dossiers) / len(red_dossiers)
        if red_dossiers else 0.0
    )

    if red_count >= 3 and avg_red_risk >= 4.5:
        return "🔴 CRITICAL", "production 배포 보류 권고 — RED 노드 다수 + aggregate risk ≥4.5"
    if red_count >= 2:
        return "🔴 HIGH", "Must-Fix mitigation 모두 적용 후 단계적 배포 권고"
    if red_count >= 1 or yellow_count >= 3:
        return "🟡 MEDIUM", "Targeted mitigation + monitoring 적용 후 PoC 권장"
    return "🟢 LOW", "Standard governance만으로 배포 가능"


# =============================================================
# Failure mode aggregation
# =============================================================

def top_failure_modes(dossiers: list[NodeMitigationDossier], top_n: int = 3) -> list[dict]:
    """
    모든 cells의 primary failure mode를 frequency × max_risk_score로 weight해서 top-N.
    return: [{mode, count, max_risk, sample_cells}, ...]
    """
    bucket: dict[str, dict] = {}
    for d in dossiers:
        for axis_cells in d.cells_by_axis.values():
            for c in axis_cells:
                mode = c.primary or "(unspecified)"
                if not mode or mode == "(unspecified)":
                    continue
                slot = bucket.setdefault(mode, {"count": 0, "max_risk": 0.0, "cells": []})
                slot["count"] += 1
                slot["max_risk"] = max(slot["max_risk"], c.risk_score or 0.0)
                slot["cells"].append(c.cell_id)
    ranked = sorted(
        bucket.items(),
        key=lambda kv: (kv[1]["count"], kv[1]["max_risk"]),
        reverse=True,
    )
    return [
        {"mode": mode, "count": data["count"], "max_risk": round(data["max_risk"], 1), "sample_cells": data["cells"][:3]}
        for mode, data in ranked[:top_n]
    ]


# =============================================================
# Cumulative scenarios (ontology field 그대로 + cell 근거)
# =============================================================

def render_cumulative_scenarios(ontology: dict, dossiers: list[NodeMitigationDossier]) -> list[str]:
    """ontology.cumulative_scenarios 3종 + 각 dossier가 적용될 cell 근거 reference."""
    scenarios = ontology.get("cumulative_scenarios", {}) or {}
    cell_ids_all = [
        c.cell_id
        for d in dossiers
        for cells in d.cells_by_axis.values()
        for c in cells
    ]
    lines: list[str] = []
    label_map = {
        "minimum_safe": "Minimum (Must Fix only)",
        "balanced": "Balanced (Must Fix + Recommend)",
        "maximum_safety": "Maximum Safety (모두 적용)",
    }
    for key, label in label_map.items():
        sc = scenarios.get(key, {})
        if not sc:
            continue
        desc = sc.get("description", "")
        delta = sc.get("total_risk_reduction_estimate", "—")
        suitable = ", ".join(sc.get("suitable_for", []) or [])
        # cell 근거: 본 sample dossier들의 cell_id 일부 첨부 (full list는 heatmap)
        ref = ", ".join(cell_ids_all[:6]) + (f", ... (+{len(cell_ids_all)-6})" if len(cell_ids_all) > 6 else "")
        lines.append(
            f"- **{label}** · risk Δ {delta} · _{desc.strip().splitlines()[0] if desc else ''}_\n"
            f"  - 적합: {suitable or '—'}\n"
            f"  - cell 근거 ({len(cell_ids_all)}개): `{ref}`"
        )
    return lines


# =============================================================
# Next action — RED top-1 must_fix 첫 step
# =============================================================

def first_next_action(dossiers: list[NodeMitigationDossier]) -> str:
    """
    다음 액션 선택 순서:
      1. RED 노드 중 aggregate 최고 1개 pick
      2. axis 우선순위: general_failure → handoff → security
         (CDO/CIO 친화: 기능적 root cause 우선, 보안 다음, handoff는 본인 IP 영역으로 마지막 fallback)
      3. placeholder option ('see external reference' 시작) skip
      4. 동일 axis 내 risk_delta 최대 옵션 select
    """
    red = [d for d in dossiers if d.color == "RED"]
    if not red:
        return "(RED 노드 부재 — monitoring 단계로 진입)"
    top = sorted(red, key=lambda d: d.aggregate_risk, reverse=True)[0]
    axis_priority = ("general_failure", "handoff", "security")
    for axis in axis_priority:
        for c in top.cells_by_axis.get(axis, []):
            real_options = [
                o for o in c.options
                if o.tier == "must_fix" and not o.action.startswith("(see external reference")
            ]
            if not real_options:
                continue
            o = max(real_options, key=lambda x: x.risk_delta)
            return (
                f"노드 **{top.node_id}** (aggregate risk {top.aggregate_risk}) — "
                f"_{c.primary or 'primary failure'}_ 대응:\n"
                f"  > {o.action}\n"
                f"  (cell `{c.cell_id}`, axis {axis}, Δrisk -{o.risk_delta}, "
                f"cost {o.cost}/5, impl {o.impl_effort}/5)"
            )
    return f"노드 **{top.node_id}** — Must Fix 옵션 미정 (ontology refresh outstanding)"


# =============================================================
# Render
# =============================================================

def render_executive_summary(
    graph: TopologicalGraph,
    dossiers: list[NodeMitigationDossier],
    ontology: dict,
    title: str,
    subtitle: str = "",
) -> str:
    red_nodes = [n for n in graph.nodes if n.color == "RED"]
    yellow_nodes = [n for n in graph.nodes if n.color == "YELLOW"]
    grade, grade_note = grade_workflow(graph, dossiers)
    failure_top = top_failure_modes(dossiers, top_n=3)
    scenario_lines = render_cumulative_scenarios(ontology, dossiers)
    next_action_text = first_next_action(dossiers)

    # RED 노드별 primary failure mode 1줄
    red_lines: list[str] = []
    dossier_by_id = {d.node_id: d for d in dossiers}
    for n in red_nodes:
        d = dossier_by_id.get(n.id)
        primary = "(no cell match)"
        if d:
            gf = d.cells_by_axis.get("general_failure", [])
            if gf and gf[0].primary:
                primary = gf[0].primary
        red_lines.append(f"  - **{n.id}** {n.label} — `{primary}` (aggregate risk {d.aggregate_risk if d else '—'})")

    # failure top-3 lines
    failure_lines: list[str] = []
    for i, fm in enumerate(failure_top, start=1):
        cells_ref = ", ".join(f"`{c}`" for c in fm["sample_cells"])
        failure_lines.append(
            f"  {i}. **{fm['mode']}** — {fm['count']} cells · max risk {fm['max_risk']}/5 · 예: {cells_ref}"
        )

    today = datetime.now().strftime("%Y-%m-%d")
    md = f"""# Executive Summary — {title}

> **진단일**: {today} · **Engine**: FDE Agent v0.1 · **Ontology**: v0.3c (36 cells)
> {subtitle}

---

## a. 전체 워크플로우 위험 등급

**{grade}** — {grade_note}

- 노드 분포: 🔴 RED {len(red_nodes)} · 🟡 YELLOW {len(yellow_nodes)} · 🟢 GREEN {len(graph.nodes) - len(red_nodes) - len(yellow_nodes)} · 총 {len(graph.nodes)}
- 평균 RED aggregate risk: **{round(sum(d.aggregate_risk for d in dossiers if d.color == 'RED') / max(1, len([d for d in dossiers if d.color == 'RED'])), 2)}/5**

## b. RED 노드 ({len(red_nodes)}개) — 핵심 failure mode

{chr(10).join(red_lines) if red_lines else '  - (RED 노드 없음)'}

## c. 핵심 Failure Mode Top-3 (frequency × impact)

{chr(10).join(failure_lines) if failure_lines else '  - (집계 가능 cell 없음)'}

## d. Cumulative Scenario 권고 (ontology v0.3c)

{chr(10).join(scenario_lines) if scenario_lines else '- (ontology cumulative_scenarios 미정)'}

## e. 추정 ROI

**AI 도입 전 사전 진단 — 평균 수억 원 매몰비용 회피.**
근거: MIT NANDA 2025 GenAI 파일럿 95% 실패, RAND 80.3% ROI 미달, Gartner 2025년 42% 기업이 AI initiative ≥1개 폐기 (전년 17% → 2.5배). 본 진단은 *설계도 단계*에서 RED 노드를 식별해 매몰비용 발생 전 차단.

## f. 다음 액션 (3-step playbook 첫 단계)

{next_action_text}

> 전체 dossier + Multi-Option mitigations는 동봉된 interactive heatmap HTML 참조.
"""
    return md


def render_to_file(
    graph: TopologicalGraph,
    dossiers: list[NodeMitigationDossier],
    ontology: dict,
    output_path: str | Path,
    title: str,
    subtitle: str = "",
) -> Path:
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_executive_summary(graph, dossiers, ontology, title, subtitle)
    out_path.write_text(md, encoding="utf-8")
    return out_path


if __name__ == "__main__":
    # ============================================================
    # E2E — legal + loan executive summary generation
    # ============================================================
    from pathlib import Path

    from ..parser import parse_markdown_with_mermaid

    SCRIPT_ROOT = Path(__file__).parent.parent
    SAMPLES_DIR = SCRIPT_ROOT / "data" / "sample-workflows"
    OUTPUT_DIR = SCRIPT_ROOT / "output"

    SAMPLE_SPECS = [
        {
            "key": "legal",
            "sample_source": "legal",
            "md_path": SAMPLES_DIR / "legal-contract-review-v0.1.md",
            "title": "Vendor Contract Review",
            "subtitle": "Global horizontal · 9 nodes · high-stakes legal liability",
        },
        {
            "key": "loan",
            "sample_source": "korean_loan",
            "md_path": SAMPLES_DIR / "loan-underwriting-kr-v0.1.md",
            "title": "Korean Personal Loan Underwriting",
            "subtitle": "한국 금융 vertical · K-PIPA / KoFIU / 공정대출법 / Article 22-2",
        },
    ]

    rec = SubAgent5MitigationRecommender()
    print(f"[init] ontology {rec.version}, cells={len(rec.cells)}")
    for spec in SAMPLE_SPECS:
        graph = parse_markdown_with_mermaid(
            spec["md_path"].read_text(encoding="utf-8"),
            sample_id=spec["md_path"].stem,
        )
        dossiers = [
            rec.diagnose_node(n.id, spec["sample_source"], n.color)
            for n in graph.red_yellow_nodes()
        ]
        out_path = OUTPUT_DIR / f"{spec['key']}-executive-summary-v0.1.md"
        rendered = render_to_file(
            graph=graph,
            dossiers=dossiers,
            ontology=rec.ontology,
            output_path=out_path,
            title=spec["title"],
            subtitle=spec["subtitle"],
        )
        lines = len(rendered.read_text(encoding="utf-8").split("\n"))
        kb = rendered.stat().st_size / 1024
        print(f"  {spec['key']}: {rendered.name} ({kb:.1f} KB, {lines} lines)")
    print("DONE.")
