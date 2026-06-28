"""
FDE Agent — Executive Summary Generator (Layer 3 Render)

Input: TopologicalGraph + per-node NodeMitigationDossier list + ontology (cumulative_scenarios used)
Output: Markdown 1 page (~30~50 lines), CDO/CIO-friendly layout
Output file: scripts/output/{sample}-executive-summary-v0.1.md

Sections (architecture.md §8 RENDER 'executive summary' component):
  a. Overall risk grade (CRITICAL / HIGH / MEDIUM / LOW)
  b. RED node list (N nodes) + core failure mode in one line
  c. Core failure mode top-3 (by frequency + impact)
  d. Cumulative Scenario recommendation (Minimum / Balanced / Maximum Safety) — ontology field as-is + cell evidence
  e. Estimated ROI in one line
  f. Next action (first step of 3-step playbook)

Prioritizing readability for the Devpost video final 30-second hero shot.
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
    """Return overall workflow risk grade + one-line description."""
    red_count = sum(1 for n in graph.nodes if n.color == "RED")
    yellow_count = sum(1 for n in graph.nodes if n.color == "YELLOW")
    red_dossiers = [d for d in dossiers if d.color == "RED"]
    avg_red_risk = (
        sum(d.aggregate_risk for d in red_dossiers) / len(red_dossiers)
        if red_dossiers else 0.0
    )

    if red_count >= 3 and avg_red_risk >= 4.5:
        return "🔴 CRITICAL", "Recommend halting production deployment — multiple RED nodes + aggregate risk ≥4.5"
    if red_count >= 2:
        return "🔴 HIGH", "Recommend staged deployment after applying all Must-Fix mitigations"
    if red_count >= 1 or yellow_count >= 3:
        return "🟡 MEDIUM", "Recommend PoC after applying targeted mitigations + monitoring"
    return "🟢 LOW", "Deployable with standard governance only"


# =============================================================
# Failure mode aggregation
# =============================================================

def top_failure_modes(dossiers: list[NodeMitigationDossier], top_n: int = 3) -> list[dict]:
    """
    Weight all cells' primary failure modes by frequency × max_risk_score and return top-N.
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
# Cumulative scenarios (ontology field as-is + cell evidence)
# =============================================================

def render_cumulative_scenarios(ontology: dict, dossiers: list[NodeMitigationDossier]) -> list[str]:
    """Three ontology.cumulative_scenarios + cell evidence reference for each applicable dossier."""
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
        "maximum_safety": "Maximum Safety (all applied)",
    }
    for key, label in label_map.items():
        sc = scenarios.get(key, {})
        if not sc:
            continue
        desc = sc.get("description", "")
        delta = sc.get("total_risk_reduction_estimate", "—")
        suitable = ", ".join(sc.get("suitable_for", []) or [])
        # cell evidence: attach a subset of cell_ids from sample dossiers (full list in heatmap)
        ref = ", ".join(cell_ids_all[:6]) + (f", ... (+{len(cell_ids_all)-6})" if len(cell_ids_all) > 6 else "")
        lines.append(
            f"- **{label}** · risk Δ {delta} · _{desc.strip().splitlines()[0] if desc else ''}_\n"
            f"  - Suitable for: {suitable or '—'}\n"
            f"  - Cell evidence ({len(cell_ids_all)} cells): `{ref}`"
        )
    return lines


# =============================================================
# Next action — first must_fix step from the top RED node
# =============================================================

def first_next_action(dossiers: list[NodeMitigationDossier]) -> str:
    """
    Next action selection order:
      1. Pick the single RED node with the highest aggregate risk
      2. Axis priority: general_failure → handoff → security
         (CDO/CIO-friendly: functional root cause first, security next, handoff as last fallback for IP-domain issues)
      3. Skip placeholder options (starting with 'see external reference')
      4. Select the option with the highest risk_delta within the same axis
    """
    red = [d for d in dossiers if d.color == "RED"]
    if not red:
        return "(No RED nodes — entering monitoring phase)"
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
                f"Node **{top.node_id}** (aggregate risk {top.aggregate_risk}) — "
                f"addressing _{c.primary or 'primary failure'}_:\n"
                f"  > {o.action}\n"
                f"  (cell `{c.cell_id}`, axis {axis}, Δrisk -{o.risk_delta}, "
                f"cost {o.cost}/5, impl {o.impl_effort}/5)"
            )
    return f"Node **{top.node_id}** — Must Fix option not yet defined (ontology refresh outstanding)"


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

    # One-line primary failure mode per RED node
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
            f"  {i}. **{fm['mode']}** — {fm['count']} cells · max risk {fm['max_risk']}/5 · e.g. {cells_ref}"
        )

    today = datetime.now().strftime("%Y-%m-%d")
    md = f"""# Executive Summary — {title}

> **Diagnosis date**: {today} · **Engine**: FDE Agent v0.1 · **Ontology**: v0.3c (36 cells)
> {subtitle}

---

## a. Overall Workflow Risk Grade

**{grade}** — {grade_note}

- Node distribution: 🔴 RED {len(red_nodes)} · 🟡 YELLOW {len(yellow_nodes)} · 🟢 GREEN {len(graph.nodes) - len(red_nodes) - len(yellow_nodes)} · Total {len(graph.nodes)}
- Average RED aggregate risk: **{round(sum(d.aggregate_risk for d in dossiers if d.color == 'RED') / max(1, len([d for d in dossiers if d.color == 'RED'])), 2)}/5**

## b. RED Nodes ({len(red_nodes)}) — Core Failure Mode

{chr(10).join(red_lines) if red_lines else '  - (No RED nodes)'}

## c. Core Failure Mode Top-3 (frequency × impact)

{chr(10).join(failure_lines) if failure_lines else '  - (No aggregatable cells found)'}

## d. Cumulative Scenario Recommendation (ontology v0.3c)

{chr(10).join(scenario_lines) if scenario_lines else '- (ontology cumulative_scenarios not yet defined)'}

## e. Estimated ROI

**Pre-deployment diagnosis before AI adoption — avoids sunk costs averaging hundreds of millions of KRW.**
Basis: MIT NANDA 2025 GenAI pilots 95% failure rate, RAND 80.3% ROI shortfall, Gartner 42% of enterprises in 2025 abandoned ≥1 AI initiative (vs. 17% prior year, 2.5× increase). This diagnosis identifies RED nodes at the *design stage*, blocking sunk costs before they occur.

## f. Next Action (first step of 3-step playbook)

{next_action_text}

> See the attached interactive heatmap HTML for the full dossier + Multi-Option mitigations.
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
            "subtitle": "Korean financial vertical · K-PIPA / KoFIU / Fair Lending Act / Article 22-2",
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
