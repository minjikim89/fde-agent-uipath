"""
Sub-Agent 1~5 e2e dry-run with Gemini brain (or Mock/Claude fallback).

산출:
  - scripts/output/diagnosis-v0.4-{legal,loan}.md

Pipeline:
  SA1 parse (markdown) → graph
  → SA2 risk diagnosis (ontology lookup + Gemini inference for unmatched)
  → SA3 standards mapping (OWASP/MITRE ontology + Gemini NIST/EU)
  → SA4 AIID RAG (Chroma retrieve + Gemini query rewriting)
  → SA5 mitigation (compose 기존 recommender + Gemini MIT 831 보강)
  → aggregator (final score + handoff metric boost demo)

regression: 기존 diagnosis-v0.2 의 final score와 ±0.3 이내 정합 (mock brain일 경우 ±0.0).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import (
    SubAgent1ParserGemini, SubAgent2RiskGemini, SubAgent3StandardsGemini,
    SubAgent4RAGGemini, SubAgent5MitigationGemini,
)
from ..brain_factory import get_brain
from ..aggregator import aggregate_node
from ...heatmap.render import _demo_handoff_metrics


SCRIPT_ROOT = Path(__file__).parent.parent.parent
SAMPLES_DIR = SCRIPT_ROOT / "data" / "sample-workflows"
OUTPUT_DIR = SCRIPT_ROOT / "output"


SAMPLE_SPECS = [
    {
        "key": "legal",
        "sample_source": "legal",
        "md_path": SAMPLES_DIR / "legal-contract-review-v0.1.md",
        "title": "Vendor Contract Review",
    },
    {
        "key": "loan",
        "sample_source": "korean_loan",
        "md_path": SAMPLES_DIR / "loan-underwriting-kr-v0.1.md",
        "title": "Korean Personal Loan Underwriting",
    },
]


def render_diagnosis_md(
    sample_key: str,
    title: str,
    sample_source: str,
    brain_info: dict,
    graph,
    diagnoses: list[dict],
    standards: list[dict],
    rag: list[dict],
    mitigations: list[dict],
    aggregated: dict,
) -> str:
    lines: list[str] = []
    lines.append(f"# Diagnosis v0.4 — {title} ({sample_key})")
    lines.append("")
    lines.append(f"> **Brain**: `{brain_info['name']}` (model `{brain_info['model']}`, ready={brain_info['ready']})")
    lines.append(f"> **Sample**: {sample_source} · **Generated**: {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"> **Pipeline**: SA1 parser → SA2 risk → SA3 standards → SA4 RAG → SA5 mitigation → aggregator")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## Pipeline summary")
    lines.append(f"- SA1 parsed: **{len(graph.nodes)} nodes**, {len(graph.edges)} edges · RED={[n.id for n in graph.red_nodes()]} · YELLOW={[n.id for n in graph.nodes if n.color=='YELLOW']}")
    lines.append(f"- SA2 diagnosed: **{len(diagnoses)} dossiers** (RED+YELLOW)")
    lines.append(f"- SA3 standards: ontology(OWASP+MITRE) + LLM(NIST+EU) — total {sum(len(s['owasp_llm_top10'])+len(s['mitre_atlas'])+len(s['nist_ai_rmf'])+len(s['eu_ai_act']) for s in standards)} mappings")
    lines.append(f"- SA4 RAG: {sum(len(r['incidents']) for r in rag)} AIID incidents retrieved (chroma_ready={rag[0]['chroma_ready'] if rag else False})")
    lines.append(f"- SA5 mitigation: {sum(sum(len(cells) for cells in m['cells_by_axis'].values()) for m in mitigations)} ontology cells · {sum(len(m['gemini_extras']) for m in mitigations)} gemini extra mitigations")
    lines.append("")

    # per-RED node section
    standards_by_id = {s["node_id"]: s for s in standards}
    rag_by_id = {r["node_id"]: r for r in rag}
    mit_by_id = {m["node_id"]: m for m in mitigations}

    for n in graph.red_yellow_nodes():
        if n.color != "RED":
            continue
        agg = aggregated.get(n.id)
        lines.append(f"## RED · {n.id} — {n.label}")
        if agg:
            ax = agg.axis_scores
            lines.append(f"- **Final risk**: {agg.final_score}/5 → **{agg.color}**")
            lines.append(f"- Axis breakdown: general {ax['general_failure']} · security {ax['security']} · handoff {ax['handoff_base']}→{ax['handoff_with_boost']} (+{agg.runtime_metric_boost} boost)")
            if agg.runtime_metric_alerts:
                lines.append(f"- Runtime alerts: {', '.join(agg.runtime_metric_alerts)}")
        st = standards_by_id.get(n.id, {})
        if st:
            ow = ", ".join(f"{t['id']}" for t in st["owasp_llm_top10"][:3]) or "—"
            mi = ", ".join(f"{t['id']}" for t in st["mitre_atlas"][:3]) or "—"
            nist = ", ".join(f"{t.get('function','?')}/{t.get('subcategory','')}" for t in st["nist_ai_rmf"][:2]) or "—"
            eu = ", ".join(f"Annex {t.get('annex_iii_category','?')}" for t in st["eu_ai_act"][:2]) or "—"
            lines.append(f"- Standards: OWASP({ow}) · MITRE({mi}) · NIST({nist}) · EU({eu}) · src={st['source']}")
        rg = rag_by_id.get(n.id, {})
        if rg and rg.get("incidents"):
            inc_lines = "\n".join(f"    - ({i['similarity']:.3f}) `{i['id']}` {i['title'][:80]}" for i in rg["incidents"][:3])
            lines.append(f"- RAG (query: _{rg['query'][:80]}_):\n{inc_lines}")
        mit = mit_by_id.get(n.id, {})
        if mit:
            extras = mit.get("gemini_extras", [])
            lines.append(f"- Mitigation: {sum(sum(len(c['options']) for c in cells) for cells in mit['cells_by_axis'].values())} ontology options + {len(extras)} gemini extras")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Regression check vs diagnosis-v0.2 (Claude/sprint-1 baseline)")
    lines.append("See `_research/2026-05-26-brain-comparison.md` for per-node Δ analysis.")
    return "\n".join(lines)


def run_one_sample(spec: dict) -> dict:
    brain = get_brain()
    sa1 = SubAgent1ParserGemini(brain=brain)
    sa2 = SubAgent2RiskGemini(brain=brain)
    sa3 = SubAgent3StandardsGemini(brain=brain)
    sa4 = SubAgent4RAGGemini(brain=brain, n_results=3)
    sa5 = SubAgent5MitigationGemini(brain=brain)

    print(f"\n=== {spec['key']} → e2e (brain={brain.name}) ===")
    md_text = spec["md_path"].read_text(encoding="utf-8")
    graph = sa1.parse_markdown(md_text, sample_id=spec["md_path"].stem)
    print(f"  SA1: {len(graph.nodes)} nodes, {len(graph.edges)} edges")
    diagnoses = sa2.run(graph, spec["sample_source"])
    print(f"  SA2: {len(diagnoses)} diagnoses")
    standards = sa3.run(diagnoses)
    print(f"  SA3: {len(standards)} mappings")
    rag = sa4.run(diagnoses)
    print(f"  SA4: {sum(len(r['incidents']) for r in rag)} incidents")
    mitigations = sa5.run(graph, spec["sample_source"])
    print(f"  SA5: {len(mitigations)} mitigations · gemini_extras total={sum(len(m['gemini_extras']) for m in mitigations)}")

    # aggregator + demo metrics
    handoff_by_dn = _demo_handoff_metrics(spec["key"])
    aggregated = {}
    for d in diagnoses:
        nid = d["node"]["id"]
        aggregated[nid] = aggregate_node(d, handoff_metrics=handoff_by_dn.get(nid, []))
    print(f"  aggregator: {len(aggregated)} aggregated · alerts total={sum(len(a.runtime_metric_alerts) for a in aggregated.values())}")

    md = render_diagnosis_md(
        sample_key=spec["key"], title=spec["title"], sample_source=spec["sample_source"],
        brain_info=brain.healthcheck(),
        graph=graph, diagnoses=diagnoses, standards=standards,
        rag=rag, mitigations=mitigations, aggregated=aggregated,
    )
    out_path = OUTPUT_DIR / f"diagnosis-v0.4-{spec['key']}.md"
    out_path.write_text(md, encoding="utf-8")
    print(f"  rendered: {out_path.name} ({out_path.stat().st_size / 1024:.1f} KB, {len(md.splitlines())} lines)")

    return {
        "sample": spec["key"],
        "brain": brain.name,
        "aggregated_finals": {nid: a.final_score for nid, a in aggregated.items()},
    }


if __name__ == "__main__":
    results = [run_one_sample(s) for s in SAMPLE_SPECS]
    print("\n=== final score summary ===")
    for r in results:
        print(f"  {r['sample']} (brain={r['brain']}): {r['aggregated_finals']}")
    print("\nDONE.")
