"""
FDE Agent — Heatmap Renderer (HTML + Mermaid.js + D3.js)

This module accepts a TopologicalGraph + NodeMitigationDossier list and emits
a single self-contained interactive HTML file.

UI:
  - Left: BPMN flowchart rendered with Mermaid.js (uses the sample's mermaid block as-is)
    Each node has a color overlay (RED/YELLOW/GREEN) and is clickable
  - Right: dossier panel — clicking a node shows per-axis cells + 3 elements (failure_mode + evidence + mitigation)
  - Top: workflow metadata + RED/YELLOW node counts + aggregate risk distribution

External dependencies: 2 CDN scripts only (Mermaid.js + D3.js v7).
Client-side dossier lookup via embedded JSON — no additional fetch required (no server hosting needed).

Export:
  - render_heatmap_html(graph, dossiers, mermaid_src, title) -> str
  - render_to_file(graph, dossiers, mermaid_src, output_path, title)
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from ..parser import TopologicalGraph
from ..agents import NodeMitigationDossier
from ..agents.aggregator import aggregate_node, AggregatedNode
from ..metrics.ips import IPSResult
from ..metrics.confdecay import ConfDecayResult
from ..metrics.laaj import LaaJResult


# =============================================================
# Demo handoff metrics — placeholder until Phase 1 Phoenix integration
# =============================================================
#
# Actual IPS/ConfDecay/LaaJ metrics require per-node output text (Phase 1 PoC).
# This sprint embeds per-sample signature values to strengthen the video demo narrative.
# Metric values are chosen to align with the RED handoff cells in sample markdowns.

def _cd(upstream, downstream, uc, dc, band="healthy", alert=False):
    decay = round(dc - uc, 2)
    over_trust_gap = max(0.0, decay) if uc < 0.8 else 0.0
    under_use_gap = max(0.0, -decay) if uc > 0.9 else 0.0
    return ConfDecayResult(
        upstream=upstream, downstream=downstream,
        upstream_conf=uc, downstream_conf=dc, decay=decay,
        over_trust_gap=round(over_trust_gap, 2), under_use_gap=round(under_use_gap, 2),
        band=band, alert=alert,
    )


def _laaj(upstream, downstream, alignment, reasoning, flags=None, band=""):
    flags = flags or []
    return LaaJResult(
        upstream=upstream, downstream=downstream,
        alignment_score=alignment, axis_scores={},
        reasoning=reasoning, disagreement_flags=flags,
        band=band, alert=alignment < 0.6,
    )


def _demo_handoff_metrics(sample_key: str) -> dict[str, list[dict]]:
    """
    Returns: {downstream_node_id: [{ips, confdecay, laaj}, ...]}
    Legal: N3 gets ips_watch (N2→N3) + laaj_flags / N5a gets over_trust (N3→N5a) + laaj_low
    Loan : N7 gets over_trust + laaj_low (N6→N7) + ips_watch (N4→N7 SHAP) / N9 gets ips_watch (N7→N9)
    """
    if sample_key == "legal":
        return {
            "N3": [{
                "ips": IPSResult(upstream="N2", downstream="N3", score=0.55, band="watch", alert=False),
                "confdecay": _cd("N2", "N3", 0.82, 0.85, band="healthy", alert=False),
                "laaj": _laaj("N2", "N3", 0.62, "schema partial drift", ["schema token dropped"]),
            }],
            "N5a": [{
                "ips": IPSResult(upstream="N4", downstream="N5a", score=0.61, band="watch", alert=False),
                "confdecay": _cd("N3", "N5a", 0.71, 0.99, band="over_trust_alert", alert=True),
                "laaj": _laaj("N3", "N5a", 0.55, "auto-approve far exceeds upstream confidence", ["confidence cliff"]),
            }],
        }
    if sample_key == "loan":
        return {
            "N7": [
                {
                    "ips": IPSResult(upstream="N6", downstream="N7", score=0.49, band="alert", alert=True),
                    "confdecay": _cd("N6", "N7", 0.70, 0.99, band="over_trust_alert", alert=True),
                    "laaj": _laaj("N6", "N7", 0.40, "upstream fraud risk uncertainty ignored downstream",
                                  ["confidence cliff", "context lost"]),
                },
                {
                    "ips": IPSResult(upstream="N4", downstream="N7", score=0.58, band="watch", alert=False),
                    "confdecay": _cd("N4", "N7", 0.78, 0.80, band="healthy", alert=False),
                    "laaj": _laaj("N4", "N7", 0.66, "ACS feature attribution partially preserved",
                                  ["SHAP factor dropped"]),
                },
            ],
            "N9": [{
                "ips": IPSResult(upstream="N7", downstream="N9", score=0.62, band="watch", alert=False),
                "confdecay": _cd("N7", "N9", 0.80, 0.78, band="healthy", alert=False),
                "laaj": _laaj("N7", "N9", 0.71,
                              "decision reasoning summarized in letter; minor regulatory phrasing variance", []),
            }],
        }
    return {}


def build_aggregated_nodes(
    graph: TopologicalGraph,
    recommender,
    sample_source: str,
    sample_key: str,
) -> dict[str, AggregatedNode]:
    """
    Run aggregator.aggregate_node() for RED + YELLOW nodes → AggregatedNode dict.
    Returns: {node_id: AggregatedNode}
    """
    handoff_by_dn = _demo_handoff_metrics(sample_key)
    out: dict[str, AggregatedNode] = {}
    for n in graph.red_yellow_nodes():
        diagnosis = recommender.diagnosis_dict_for_node(n, sample_source)
        out[n.id] = aggregate_node(diagnosis, handoff_metrics=handoff_by_dn.get(n.id, []))
    return out


def render_heatmap_html(
    graph: TopologicalGraph,
    dossiers: list[NodeMitigationDossier],
    mermaid_src: str,
    title: str = "FDE Agent — Workflow Heatmap",
    subtitle: str = "",
    aggregated: dict[str, AggregatedNode] | None = None,
) -> str:
    """
    Embed Mermaid src + dossier JSON + aggregated (final_score + handoff metrics) and
    return a single self-contained HTML file.
    """
    dossier_by_node: dict[str, dict] = {d.node_id: d.to_dict() for d in dossiers}
    aggregated_by_node: dict[str, dict] = (
        {nid: agg.to_dict() for nid, agg in aggregated.items()} if aggregated else {}
    )
    embedded_payload = {
        "graph": graph.to_dict(),
        "dossiers": dossier_by_node,
        "aggregated": aggregated_by_node,
        "title": title,
        "subtitle": subtitle,
    }
    # Guard against `</script>` breakout — Phase 1 LaaJ reasoning is LLM-generated text
    # and may contain `</`. `\/` is equivalent to `/` in JSON, so no semantic loss.
    payload_json = json.dumps(embedded_payload, ensure_ascii=False).replace("</", "<\\/")

    # Add clickable node markers to the original Mermaid source
    # Append Mermaid.js `click NodeId callback` at the end of the mermaid src — RED/YELLOW only
    click_lines: list[str] = []
    for n in graph.red_yellow_nodes():
        click_lines.append(f'    click {n.id} call window.fdeAgentClick("{n.id}") "Click for dossier"')
    augmented_mermaid = mermaid_src.rstrip() + "\n" + "\n".join(click_lines) + "\n"

    summary = _summary_stats(graph, dossiers)

    return _HTML_TEMPLATE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        mermaid_src=augmented_mermaid,
        payload_json=payload_json,
        summary_html=summary,
    )


def render_to_file(
    graph: TopologicalGraph,
    dossiers: list[NodeMitigationDossier],
    mermaid_src: str,
    output_path: str | Path,
    title: str = "FDE Agent — Workflow Heatmap",
    subtitle: str = "",
    aggregated: dict[str, AggregatedNode] | None = None,
) -> Path:
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_str = render_heatmap_html(graph, dossiers, mermaid_src, title, subtitle, aggregated)
    out_path.write_text(html_str, encoding="utf-8")
    return out_path


def _summary_stats(graph: TopologicalGraph, dossiers: list[NodeMitigationDossier]) -> str:
    red_count = len([n for n in graph.nodes if n.color == "RED"])
    yellow_count = len([n for n in graph.nodes if n.color == "YELLOW"])
    green_count = len([n for n in graph.nodes if n.color == "GREEN"])
    total_nodes = len(graph.nodes)
    aggregate_risks = [d.aggregate_risk for d in dossiers if d.aggregate_risk]
    avg_risk = round(sum(aggregate_risks) / len(aggregate_risks), 2) if aggregate_risks else 0.0
    total_options = sum(d.summary.get("total_options", 0) for d in dossiers)
    return (
        f'<span class="stat red">🔴 RED: {red_count}</span>'
        f'<span class="stat yellow">🟡 YELLOW: {yellow_count}</span>'
        f'<span class="stat green">🟢 GREEN: {green_count}</span>'
        f'<span class="stat neutral">Total nodes: {total_nodes}</span>'
        f'<span class="stat neutral">Avg RED risk: {avg_risk}/5</span>'
        f'<span class="stat neutral">Mitigation options: {total_options}</span>'
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
    margin: 0; padding: 0; background: #f8f9fb; color: #1a1a1a;
  }}
  header {{
    padding: 16px 24px; background: white; border-bottom: 1px solid #e0e0e0;
  }}
  header h1 {{ margin: 0; font-size: 22px; }}
  header .subtitle {{ color: #666; margin-top: 4px; font-size: 14px; }}
  header .stats {{ margin-top: 12px; display: flex; gap: 12px; flex-wrap: wrap; }}
  .stat {{
    padding: 6px 12px; border-radius: 4px; font-size: 13px; font-weight: 500;
  }}
  .stat.red {{ background: #ffe5e5; color: #b00020; }}
  .stat.yellow {{ background: #fff4e0; color: #8a5500; }}
  .stat.green {{ background: #e5f5e5; color: #1b5e20; }}
  .stat.neutral {{ background: #eef0f3; color: #333; }}
  main {{
    display: grid; grid-template-columns: 1fr 460px; gap: 0; min-height: calc(100vh - 110px);
  }}
  #diagram {{
    padding: 24px; overflow: auto; background: white;
  }}
  #diagram .mermaid {{ background: white; }}
  #dossier {{
    padding: 20px; background: #fafafa; border-left: 1px solid #e0e0e0; overflow-y: auto;
    max-height: calc(100vh - 110px);
  }}
  #dossier h2 {{ margin: 0 0 4px 0; font-size: 18px; }}
  #dossier .node-meta {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
  #dossier .placeholder {{ color: #888; font-style: italic; }}
  .axis-block {{
    background: white; border: 1px solid #e0e0e0; border-radius: 6px;
    padding: 14px; margin-bottom: 14px;
  }}
  .axis-block h3 {{
    margin: 0 0 8px 0; font-size: 14px; color: #444; text-transform: uppercase;
    letter-spacing: 0.5px;
  }}
  .axis-block.axis-general_failure {{ border-left: 4px solid #d32f2f; }}
  .axis-block.axis-security {{ border-left: 4px solid #7b1fa2; }}
  .axis-block.axis-handoff {{ border-left: 4px solid #f57c00; }}
  .axis-block.axis-aggregated {{ border-left: 4px solid #1a73e8; background: #f0f6ff; }}
  .agg-row {{ font-size: 12px; margin-bottom: 6px; color: #333; }}
  .agg-row b {{ color: #444; }}
  .agg-row ul {{ margin: 4px 0 0 16px; padding: 0; font-family: 'SF Mono', 'Monaco', monospace; font-size: 11px; }}
  .agg-row code {{ background: #fff; padding: 1px 4px; border-radius: 3px; color: #b00020; }}
  .color-RED {{ color: #b00020; font-weight: 600; }}
  .color-YELLOW {{ color: #8a5500; font-weight: 600; }}
  .color-GREEN {{ color: #1b5e20; font-weight: 600; }}
  .cell {{ margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px dashed #eee; }}
  .cell:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
  .cell .primary {{
    font-weight: 600; font-size: 13px; color: #b00020;
    background: #ffe5e5; padding: 4px 8px; border-radius: 3px; display: inline-block;
    margin-bottom: 6px;
  }}
  .cell .meta {{ font-size: 12px; color: #555; margin-bottom: 6px; }}
  .cell .desc {{ font-size: 13px; color: #333; line-height: 1.5; margin-bottom: 8px; }}
  .evidence {{ font-size: 12px; color: #555; margin-bottom: 8px; }}
  .evidence ul {{ margin: 4px 0 0 16px; padding: 0; }}
  .options table {{
    width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 6px;
  }}
  .options th, .options td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; }}
  .options th {{ background: #f5f5f5; font-weight: 600; color: #555; }}
  .options tr.tier-must_fix td:first-child {{ color: #b00020; font-weight: 600; }}
  .options tr.tier-recommend td:first-child {{ color: #c2741b; font-weight: 600; }}
  .options tr.tier-optional td:first-child {{ color: #1b5e20; font-weight: 600; }}
  .options .scores {{ font-family: 'SF Mono', 'Monaco', monospace; font-size: 11px; color: #555; }}
  .option-action {{ font-size: 12px; color: #222; }}
  .option-rationale {{ font-size: 11px; color: #777; font-style: italic; margin-top: 2px; }}
  .legend {{ margin-top: 12px; font-size: 11px; color: #777; }}
  .legend code {{ background: #eef0f3; padding: 1px 4px; border-radius: 3px; }}

  /* Mermaid node colors enforced */
  .node[fill="#ffcccc"] rect, .node rect[fill="#ffcccc"] {{ fill: #ffcccc !important; }}
  .node[fill="#ffe6cc"] rect, .node rect[fill="#ffe6cc"] {{ fill: #ffe6cc !important; }}
  .node[fill="#ccffcc"] rect, .node rect[fill="#ccffcc"] {{ fill: #ccffcc !important; }}
  .node.clickable {{ cursor: pointer; }}
  .node.clickable:hover rect, .node.clickable:hover polygon {{ stroke: #1a73e8 !important; stroke-width: 3px !important; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="subtitle">{subtitle}</div>
  <div class="stats">{summary_html}</div>
</header>
<main>
  <section id="diagram">
    <div class="mermaid">
{mermaid_src}
    </div>
    <div class="legend">
      Click <code>RED</code> or <code>YELLOW</code> nodes to view per-axis dossier + Multi-Option mitigations →
    </div>
  </section>
  <aside id="dossier">
    <div class="placeholder">Click a RED/YELLOW node to view the diagnosis dossier.</div>
  </aside>
</main>

<script id="fde-payload" type="application/json">
{payload_json}
</script>

<script>
  const PAYLOAD = JSON.parse(document.getElementById("fde-payload").textContent);
  mermaid.initialize({{ startOnLoad: true, securityLevel: 'loose', flowchart: {{ useMaxWidth: true }} }});

  function tierLabel(tier) {{
    return {{ must_fix: "Must Fix", recommend: "Recommend", optional: "Optional" }}[tier] || tier;
  }}

  function renderEvidence(ev) {{
    if (!ev || Object.keys(ev).length === 0) return "";
    const parts = [];
    if (ev.aiid_incidents && ev.aiid_incidents.length) {{
      const items = ev.aiid_incidents.map(i =>
        `<li><b>${{i.id}}</b> — ${{i.title || ""}} ${{i.relevance ? `(${{i.relevance}})` : ""}}</li>`
      ).join("");
      parts.push(`<div><b>AIID:</b><ul>${{items}}</ul></div>`);
    }}
    if (ev.academic && ev.academic.length) {{
      const items = ev.academic.map(a => `<li>${{a}}</li>`).join("");
      parts.push(`<div><b>Academic:</b><ul>${{items}}</ul></div>`);
    }}
    if (ev.primary_threats && ev.primary_threats.length) {{
      const items = ev.primary_threats.map(t =>
        `<li><b>${{t.id}}</b> ${{t.title || ""}} <i>(${{t.relevance || ""}})</i></li>`
      ).join("");
      parts.push(`<div><b>OWASP threats:</b><ul>${{items}}</ul></div>`);
    }}
    if (ev.mitre_atlas_techniques && ev.mitre_atlas_techniques.length) {{
      const items = ev.mitre_atlas_techniques.map(t => `<li><b>${{t.id}}</b> ${{t.title || ""}}</li>`).join("");
      parts.push(`<div><b>MITRE ATLAS techniques:</b><ul>${{items}}</ul></div>`);
    }}
    if (ev.mitre_atlas_tactics && ev.mitre_atlas_tactics.length) {{
      const items = ev.mitre_atlas_tactics.map(t => `<li><b>${{t.id}}</b> ${{t.title || ""}}</li>`).join("");
      parts.push(`<div><b>MITRE ATLAS tactics:</b><ul>${{items}}</ul></div>`);
    }}
    return `<div class="evidence">${{parts.join("")}}</div>`;
  }}

  function renderOptions(options) {{
    if (!options || options.length === 0) {{
      return `<div class="options"><i style="font-size:12px;color:#999">(no mitigation options defined in ontology — refresh outstanding)</i></div>`;
    }}
    const rows = options.map(o => `
      <tr class="tier-${{o.tier}}">
        <td>${{tierLabel(o.tier)}}</td>
        <td>
          <div class="option-action">${{o.action}}</div>
          <div class="option-rationale">${{o.rationale}}</div>
        </td>
        <td class="scores">
          Δrisk -${{o.risk_delta}}<br>
          cost ${{o.cost}}/5<br>
          speed ${{o.speed_delta}}/5<br>
          op ${{o.op_complexity}}/5<br>
          impl ${{o.impl_effort}}/5
        </td>
      </tr>
    `).join("");
    return `
      <div class="options"><table>
        <thead><tr><th>Tier</th><th>Action + Rationale</th><th>5-dim trade-off (1-5)</th></tr></thead>
        <tbody>${{rows}}</tbody>
      </table></div>
    `;
  }}

  function renderCell(cell) {{
    return `
      <div class="cell">
        <div class="primary">⚠ ${{cell.primary || "(unspecified)"}}</div>
        <div class="meta">cell <code>${{cell.cell_id}}</code> · risk <b>${{cell.risk_score ?? "—"}}</b>${{cell.heuristic_source ? ` · ${{cell.heuristic_source}}` : ""}}</div>
        ${{cell.description ? `<div class="desc">${{cell.description.replace(/\\n/g, "<br>")}}</div>` : ""}}
        ${{renderEvidence(cell.evidence)}}
        ${{renderOptions(cell.options)}}
      </div>
    `;
  }}

  function renderAggregated(agg) {{
    if (!agg) return "";
    const alerts = (agg.runtime_metric_alerts || []).map(a => `<li><code>${{a}}</code></li>`).join("");
    const axes = agg.axis_scores || {{}};
    const boost = agg.runtime_metric_boost || 0;
    return `
      <div class="axis-block axis-aggregated">
        <h3>aggregated · final score + runtime metrics</h3>
        <div class="agg-row"><b>Final risk:</b> ${{agg.final_score}}/5 — <span class="color-${{agg.color}}">${{agg.color}}</span> (weighted: handoff 0.4 · security 0.3 · general 0.3)</div>
        <div class="agg-row"><b>Axis breakdown:</b>
          general <b>${{axes.general_failure ?? "—"}}</b> ·
          security <b>${{axes.security ?? "—"}}</b> ·
          handoff <b>${{axes.handoff_base ?? "—"}}</b>
          ${{boost > 0 ? `→ <b>${{axes.handoff_with_boost}}</b> <i>(+${{boost}} runtime boost)</i>` : ""}}
        </div>
        ${{alerts ? `<div class="agg-row"><b>IPS / ConfDecay / LaaJ alerts:</b><ul>${{alerts}}</ul></div>` : `<div class="agg-row"><i style="color:#777">no runtime metric alerts on inbound handoffs</i></div>`}}
      </div>
    `;
  }}

  function renderDossier(nodeId) {{
    const dossier = PAYLOAD.dossiers[nodeId];
    const aggregated = (PAYLOAD.aggregated || {{}})[nodeId];
    const target = document.getElementById("dossier");
    if (!dossier) {{
      target.innerHTML = `<div class="placeholder">No dossier for ${{nodeId}} (this node has no cell definition in ontology v0.3c)</div>`;
      return;
    }}
    const node = PAYLOAD.graph.nodes.find(n => n.id === nodeId);
    const axisOrder = ["general_failure", "security", "handoff"];
    const axisHtml = axisOrder.map(axis => {{
      const cells = dossier.cells_by_axis[axis] || [];
      if (cells.length === 0) return "";
      return `
        <div class="axis-block axis-${{axis}}">
          <h3>${{axis.replace("_", " ")}}</h3>
          ${{cells.map(renderCell).join("")}}
        </div>
      `;
    }}).join("");
    target.innerHTML = `
      <h2>${{nodeId}} — ${{node ? node.label : ""}}</h2>
      <div class="node-meta">
        category: <b>${{node ? node.category : "?"}}</b> ·
        AI mode: ${{node ? node.ai_mode : "?"}} ·
        ontology color: ${{dossier.color}} ·
        ontology agg risk: <b>${{dossier.aggregate_risk}}/5</b>
        ${{dossier.summary.korean_context ? ' · <i>🇰🇷 Korean context</i>' : ''}}
      </div>
      ${{renderAggregated(aggregated)}}
      ${{axisHtml || '<div class="placeholder">(no axis cells matched in ontology)</div>'}}
    `;
  }}

  window.fdeAgentClick = renderDossier;

  // Add hover styles to nodes after Mermaid renders
  document.addEventListener("DOMContentLoaded", () => {{
    setTimeout(() => {{
      Object.keys(PAYLOAD.dossiers).forEach(nid => {{
        const node = document.querySelector(`g.node[id*="-${{nid}}-"]`);
        if (node) node.classList.add("clickable");
      }});
    }}, 800);
  }});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # =========================================================
    # E2E dry-run — legal + loan sample → heatmap HTML 2 pages
    # =========================================================
    #
    # Pipeline (same for both samples):
    #   1. Mermaid parse → TopologicalGraph (includes RED/YELLOW/GREEN colors)
    #   2. Sub-Agent 5 Mitigation Recommender → per RED+YELLOW NodeMitigationDossier
    #   3. Heatmap HTML render → scripts/output/{sample}-heatmap-v0.1.html
    #
    # Korean context first: loan sample retrieves with sample_source='korean_loan'.
    import sys
    from pathlib import Path

    SCRIPT_ROOT = Path(__file__).parent.parent
    SAMPLES_DIR = SCRIPT_ROOT / "data" / "sample-workflows"
    OUTPUT_DIR = SCRIPT_ROOT / "output"

    SAMPLE_SPECS = [
        {
            "key": "legal",
            "sample_source": "legal",
            "md_path": SAMPLES_DIR / "legal-contract-review-v0.1.md",
            "title": "FDE Agent — Vendor Contract Review (v0.1)",
            "subtitle": "Layer 2 input · Global horizontal · 9 nodes · Multi-Option mitigations",
        },
        {
            "key": "loan",
            "sample_source": "korean_loan",
            "md_path": SAMPLES_DIR / "loan-underwriting-kr-v0.1.md",
            "title": "FDE Agent — Korean Personal Loan Underwriting (v0.1)",
            "subtitle": "Layer 2 input · Korean financial vertical · 11 nodes · K-PIPA / KoFIU / Fair Lending Act context",
        },
    ]

    from ..parser import parse_markdown_with_mermaid
    from ..agents import SubAgent5MitigationRecommender

    rec = SubAgent5MitigationRecommender()
    print(f"[init] ontology version = {rec.version}, cells loaded = {len(rec.cells)}")

    for spec in SAMPLE_SPECS:
        print(f"\n=== {spec['key']} → e2e ===")
        md_text = spec["md_path"].read_text(encoding="utf-8")
        graph = parse_markdown_with_mermaid(md_text, sample_id=spec["md_path"].stem)
        red_yellow = graph.red_yellow_nodes()
        print(f"  parsed: nodes={len(graph.nodes)} edges={len(graph.edges)} RED={[n.id for n in graph.red_nodes()]} YELLOW={[n.id for n in graph.nodes if n.color=='YELLOW']}")

        dossiers = []
        for n in red_yellow:
            dossier = rec.diagnose_node(n.id, spec["sample_source"], n.color)
            dossiers.append(dossier)
        print(f"  diagnosed: {len(dossiers)} dossiers (RED+YELLOW)")

        aggregated = build_aggregated_nodes(graph, rec, spec["sample_source"], spec["key"])
        agg_alerts_total = sum(len(a.runtime_metric_alerts) for a in aggregated.values())
        print(f"  aggregated: {len(aggregated)} nodes · runtime alerts total = {agg_alerts_total}")

        mermaid_src = graph.metadata.get("mermaid_raw", "")
        out_path = OUTPUT_DIR / f"{spec['key']}-heatmap-v0.1.html"
        rendered = render_to_file(
            graph=graph,
            dossiers=dossiers,
            mermaid_src=mermaid_src,
            output_path=out_path,
            title=spec["title"],
            subtitle=spec["subtitle"],
            aggregated=aggregated,
        )
        size_kb = rendered.stat().st_size / 1024
        line_count = len(rendered.read_text(encoding="utf-8").split("\n"))
        print(f"  rendered: {rendered.name} ({size_kb:.1f} KB, {line_count} lines)")

    print("\nDONE.")
