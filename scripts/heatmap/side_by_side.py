"""
FDE Agent — Side-by-Side workflow diff viz (Mermaid static)

For Phase 1 Devpost video capture. A single HTML page with left/right 2 panes:
  - Left  (Before): Original Mermaid BPMN — no color overlay, pre-diagnosis state
  - Right (After):  Must Fix mitigation applied — RED node fill switched to GREEN +
                    node label prefixed with ✓ + must_fix one-liner appended

Aligned with single-pane visual comparison preference (avoiding tab/page separation) [feedback_visual-comparison-preference].

Output: scripts/output/{legal,loan}-side-by-side-v0.1.html
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from ..parser import TopologicalGraph
from ..agents import NodeMitigationDossier


def _pick_must_fix_one_liner(dossier: NodeMitigationDossier) -> str | None:
    """Return the strongest must_fix action from a RED node dossier (axis priority order, placeholder skipped)."""
    for axis in ("general_failure", "handoff", "security"):
        for c in dossier.cells_by_axis.get(axis, []):
            for o in c.options:
                if o.tier == "must_fix" and not o.action.startswith("(see external reference"):
                    return o.action
    return None


def apply_must_fix_to_mermaid(mermaid_src: str, dossiers: list[NodeMitigationDossier]) -> tuple[str, dict[str, str]]:
    """
    For each RED node in the original Mermaid source:
      1. Change style fill from #ffcccc → #ccffcc (visualize mitigation applied)
      2. Prefix the node label with ✓ MITIGATED + append the must_fix one-liner as suffix
    return: (modified mermaid_src, {node_id: applied_action})
    """
    out = mermaid_src
    applied: dict[str, str] = {}
    for d in dossiers:
        if d.color != "RED":
            continue
        action = _pick_must_fix_one_liner(d)
        if not action:
            continue
        applied[d.node_id] = action
        # (1) Change style fill — RED → GREEN
        out = re.sub(
            rf"(style\s+{re.escape(d.node_id)}\s+fill\s*:\s*)#ffcccc",
            r"\1#ccffcc",
            out,
        )
        # (2) Update node label — prepend ✓ and append fix text inside trapezoid `Nxx[/"..."/]`
        #     Assumes each node definition appears only once in Mermaid source (verified in samples)
        snippet_fix = action[:80].replace('"', "'").replace("\n", " ")
        out = re.sub(
            rf'({re.escape(d.node_id)}\[/")(.+?)("/\])',
            lambda m, fix=snippet_fix: f'{m.group(1)}✓ MITIGATED — {m.group(2)}<br/><i>fix: {fix}</i>{m.group(3)}',
            out,
            count=1,
            flags=re.DOTALL,
        )
    return out, applied


def render_side_by_side_html(
    graph: TopologicalGraph,
    dossiers: list[NodeMitigationDossier],
    mermaid_src: str,
    title: str = "FDE Agent — Workflow Before / After",
    subtitle: str = "",
) -> str:
    after_mermaid, applied = apply_must_fix_to_mermaid(mermaid_src, dossiers)
    red_count = sum(1 for n in graph.nodes if n.color == "RED")
    yellow_count = sum(1 for n in graph.nodes if n.color == "YELLOW")
    applied_lines = "".join(
        f"<li><b>{nid}</b> — {html.escape(action[:120])}</li>"
        for nid, action in applied.items()
    )
    if not applied_lines:
        applied_lines = "<li><i>(No ontology cell with a Must Fix option defined for this RED node)</i></li>"

    return _TEMPLATE.format(
        title=html.escape(title),
        subtitle=html.escape(subtitle),
        red_count=red_count,
        yellow_count=yellow_count,
        applied_count=len(applied),
        before_mermaid=mermaid_src,
        after_mermaid=after_mermaid,
        applied_lines=applied_lines,
    )


def render_to_file(
    graph: TopologicalGraph,
    dossiers: list[NodeMitigationDossier],
    mermaid_src: str,
    output_path: str | Path,
    title: str = "FDE Agent — Workflow Before / After",
    subtitle: str = "",
) -> Path:
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_str = render_side_by_side_html(graph, dossiers, mermaid_src, title, subtitle)
    out_path.write_text(html_str, encoding="utf-8")
    return out_path


_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
    margin: 0; padding: 0; background: #f8f9fb; color: #1a1a1a;
  }}
  header {{ padding: 16px 24px; background: white; border-bottom: 1px solid #e0e0e0; }}
  header h1 {{ margin: 0; font-size: 22px; }}
  header .subtitle {{ color: #666; margin-top: 4px; font-size: 14px; }}
  header .stats {{ margin-top: 12px; display: flex; gap: 12px; flex-wrap: wrap; font-size: 13px; }}
  .stat {{ padding: 6px 12px; border-radius: 4px; font-weight: 500; }}
  .stat.red {{ background: #ffe5e5; color: #b00020; }}
  .stat.yellow {{ background: #fff4e0; color: #8a5500; }}
  .stat.green {{ background: #e5f5e5; color: #1b5e20; }}
  main {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0; min-height: calc(100vh - 220px); }}
  section {{ padding: 18px 22px; overflow: auto; background: white; border-right: 1px solid #e0e0e0; }}
  section.after {{ border-right: none; background: #f7fcf7; }}
  section h2 {{ margin: 0 0 4px 0; font-size: 18px; }}
  section h2 .badge {{
    font-size: 11px; padding: 2px 8px; border-radius: 12px; font-weight: 500;
    vertical-align: middle; margin-left: 8px;
  }}
  section.before h2 .badge {{ background: #ffe5e5; color: #b00020; }}
  section.after  h2 .badge {{ background: #e5f5e5; color: #1b5e20; }}
  section .caption {{ color: #666; font-size: 13px; margin-bottom: 14px; }}
  .mermaid {{ background: white; }}
  section.after .mermaid {{ background: #f7fcf7; }}
  footer {{
    padding: 14px 24px; background: white; border-top: 1px solid #e0e0e0;
    font-size: 13px; color: #333;
  }}
  footer h3 {{ margin: 0 0 8px 0; font-size: 14px; }}
  footer ul {{ margin: 0; padding-left: 20px; }}
  footer li {{ margin-bottom: 4px; }}
  .video-hint {{
    margin-top: 8px; padding: 8px 12px; background: #f0f6ff;
    border-left: 4px solid #1a73e8; color: #444; font-size: 12px;
    border-radius: 3px;
  }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="subtitle">{subtitle}</div>
  <div class="stats">
    <span class="stat red">🔴 RED: {red_count}</span>
    <span class="stat yellow">🟡 YELLOW: {yellow_count}</span>
    <span class="stat green">✓ Must Fix applied: {applied_count}</span>
  </div>
</header>
<main>
  <section class="before">
    <h2>Before <span class="badge">Pre-diagnosis — Original BPMN</span></h2>
    <div class="caption">Workflow submitted by the client. Risk in AI nodes unknown.</div>
    <div class="mermaid">
{before_mermaid}
    </div>
  </section>
  <section class="after">
    <h2>After <span class="badge">Must Fix Applied</span></h2>
    <div class="caption">FDE Agent diagnosis applied one Must Fix per RED node. RED → GREEN transition.</div>
    <div class="mermaid">
{after_mermaid}
    </div>
  </section>
</main>
<footer>
  <h3>Applied Must Fix Mitigations (per RED node)</h3>
  <ul>
    {applied_lines}
  </ul>
  <div class="video-hint">
    Devpost capture hint: Capture both Before/After panes in a single shot. RED → GREEN color change + ✓ MITIGATED label visible at a glance.
    Recommend + Optional options can be further demonstrated in the dossier panel of the interactive heatmap HTML.
  </div>
</footer>
<script>
  mermaid.initialize({{ startOnLoad: true, securityLevel: 'loose', flowchart: {{ useMaxWidth: true }} }});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    # E2E — legal + loan side-by-side HTML 2 pages
    from ..parser import parse_markdown_with_mermaid
    from ..agents import SubAgent5MitigationRecommender

    SCRIPT_ROOT = Path(__file__).parent.parent
    SAMPLES_DIR = SCRIPT_ROOT / "data" / "sample-workflows"
    OUTPUT_DIR = SCRIPT_ROOT / "output"

    SAMPLES = [
        {
            "key": "legal",
            "sample_source": "legal",
            "md_path": SAMPLES_DIR / "legal-contract-review-v0.1.md",
            "title": "FDE Agent — Vendor Contract Review · Before / After",
            "subtitle": "Phase 1 Devpost capture · Must Fix mitigation visualization",
        },
        {
            "key": "loan",
            "sample_source": "korean_loan",
            "md_path": SAMPLES_DIR / "loan-underwriting-kr-v0.1.md",
            "title": "FDE Agent — Korean Loan Underwriting · Before / After",
            "subtitle": "Korean financial vertical · K-PIPA / Fair Lending Act / Article 22-2 Must Fix applied",
        },
    ]

    rec = SubAgent5MitigationRecommender()
    print(f"[init] ontology {rec.version}, cells={len(rec.cells)}")
    for spec in SAMPLES:
        md_text = spec["md_path"].read_text(encoding="utf-8")
        graph = parse_markdown_with_mermaid(md_text, sample_id=spec["md_path"].stem)
        dossiers = [
            rec.diagnose_node(n.id, spec["sample_source"], n.color)
            for n in graph.red_yellow_nodes()
        ]
        mermaid_src = graph.metadata.get("mermaid_raw", "")
        out_path = OUTPUT_DIR / f"{spec['key']}-side-by-side-v0.1.html"
        rendered = render_to_file(
            graph=graph,
            dossiers=dossiers,
            mermaid_src=mermaid_src,
            output_path=out_path,
            title=spec["title"],
            subtitle=spec["subtitle"],
        )
        kb = rendered.stat().st_size / 1024
        line_count = len(rendered.read_text(encoding="utf-8").splitlines())
        print(f"  {spec['key']}: {rendered.name} ({kb:.1f} KB, {line_count} lines)")
    print("DONE.")
