"""
Gemini Critic Disagreement Heatmap (Wave 2 · Rapid path)
========================================================

Side-by-side Gemini primary vs Gemini critic (self-critique) per-node
disagreement visualization for the Rapid Agent Devpost 영상 1:15-1:35
timeline + meta narrative "We diagnose AI workflows — our own diagnosis
is self-diagnosed by a same-family adversarial 2nd-pass (Gemini Pro
critic ↔ Gemini primary, or Flash↔Pro)".

Model policy (architecture.md §2 Model Policy, 2026-05-29):
  - Rapid Agent = Gemini-only. Sub-Agent 6 = Gemini self-critique.
  - UiPath path = multi-model (Claude ↔ Gemini) — owns a separate viz file.

Reads `_peer_review_fixture.py` (baseline, untouched) and emits a static
self-contained HTML per workflow:

  scripts/output/peer-review-disagreement-legal-v0.1.html
  scripts/output/peer-review-disagreement-loan-v0.1.html

Static frame: 1920×1080, font ≥16pt, color-vision-friendly palette
(Wong 2011-style ColorBrewer pairing — flagged warm-red #B91C1C against
consensus cool-green #166534, distinguishable for deuteranopia/protanopia).

Why a separate compute_claude_implied_risk() helper (function name preserved as code identifier; UI label = Gemini critic)
------------------------------------------------------------------------------
Fixture exposes the critic's `axis_scores` (alignment / coverage /
hallucination_risk) which score *critic's confidence in the primary
diagnosis* — orthogonal to the node's intrinsic risk magnitude.
A GREEN node where the Gemini critic broadly concurs has axis_scores=5/5/5
(="primary's low-risk verdict is well-supported"), not because the node
itself is risky.

To produce a critic-implied risk on the [0, 5] scale comparable to
Gemini primary's aggregate_risk, we derive from three deterministic signals
the fixture *does* expose:

  1. flags empty + "concur" in alternative_view → implied ≈ gemini  (no drift)
  2. alternative_view keyword patterns drift the score:
       "borderline" / "YELLOW"   → critic thinks primary *over*-scored
       "drift" / "off-vertical"  → RAG evidence weak, lower implied
       "missing" / "gap" / "thin"→ coverage incomplete, lower implied
  3. flag count adds magnitude — each flag drifts -0.1

This keeps the fixture as the raw signal source and the *interpretation*
(critic-implied risk) as a viz concern. Future swap to real Gemini Pro
critic API output replaces only the fixture; viz logic survives untouched.

Code-identifier preservation note
---------------------------------
Function names (`compute_claude_implied_risk`, `claude_delta`,
`claude_flagged`), fixture dict keys (`node["claude"]`), and Phoenix
attribute keys (`fde.peer.claude_score`) are preserved as **code
identifiers** even though the model is now Gemini self-critique. Renaming
would break dashboard_config.json attribute freeze and fixture API. UI
labels are the only swap.

Phoenix 5-metric overlay
------------------------
Per-node span attribute  : fde.peer.flagged              (bool, abs(Δ) > 0.5)
Per-node span attribute  : fde.peer.delta                (float, signed)
Workflow crew-root attr  : fde.peer.disagreement_count   (int, ★ 5th metric)

See scripts/observability/phoenix/dashboard_config.json §peer_review.
"""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from _peer_review_fixture import (  # type: ignore  # noqa: E402
    FIXTURE,
    DELTA_FLAG_THRESHOLD,
    peer_confidence,
    mitigation_diff,
    evidence_diff,
)


REPO_SCRIPTS = SCRIPT_DIR.parent
OUTPUT_DIR = REPO_SCRIPTS / "output"


# ---------------------------------------------------------------------------
# Gemini critic implied risk — viz-layer derivation (★ fixture is read-only)
# (function name compute_claude_implied_risk preserved as code identifier)
# ---------------------------------------------------------------------------

LOWER_DRIFT_PATTERNS = {
    "borderline": -0.5,
    "yellow": -0.4,        # peer reading "could be YELLOW"
    "drifted off": -0.6,
    "off-vertical": -0.6,
    "off-domain": -0.5,
    "off-topic": -0.5,
    "missing": -0.4,
    "gap": -0.3,
    "thin": -0.3,
    "implicit": -0.2,
    "borderline.": -0.5,
}

CONCUR_TOKENS = ("concur with primary", "concur with the primary", "concur with rate",
                 "concur red", "concur yellow", "concur green",
                 "concur broadly", "concur — ", "concur —")


def compute_claude_implied_risk(node: dict[str, Any]) -> float:
    """
    Derive Gemini critic's implied risk score in [0, 5], same scale as
    gemini.aggregate_risk (primary).

    Deterministic — depends only on fixture fields (alternative_view text +
    disagreement_flags count). Mock fixture path; real path swap = Gemini Pro
    critic vs primary (Rapid Model Policy: Gemini-only).

    Function name retained as code identifier for fixture/dashboard freeze.
    """
    gemini = float(node["gemini"]["aggregate_risk"])
    flags: list[str] = node["claude"]["disagreement_flags"]
    alt: str = node["claude"]["alternative_view"].lower()

    # Path A — full concur + no flags ⇒ implied ≈ gemini (zero drift)
    if not flags and any(tok in alt for tok in CONCUR_TOKENS):
        return round(gemini, 2)

    # Path B — pattern-based drift accumulation (idempotent per pattern)
    drift = 0.0
    matched: set[str] = set()
    for pattern, weight in LOWER_DRIFT_PATTERNS.items():
        if pattern in alt and pattern not in matched:
            drift += weight
            matched.add(pattern)

    # Flag count adds magnitude (each surfaced concern drifts -0.1)
    drift += -0.1 * len(flags)

    implied = round(max(0.0, min(5.0, gemini + drift)), 2)
    return implied


def claude_delta(node: dict) -> float:
    """gemini.aggregate_risk (primary) - gemini_critic_implied_risk (signed)."""
    return round(float(node["gemini"]["aggregate_risk"]) - compute_claude_implied_risk(node), 2)


def claude_flagged(node: dict) -> bool:
    return abs(claude_delta(node)) > DELTA_FLAG_THRESHOLD


def viz_workflow_summary(sample: str) -> dict:
    """Recomputed summary using viz-layer implied risk (fixture summary is stale)."""
    nodes = FIXTURE[sample]
    flagged = [n for n in nodes if claude_flagged(n)]
    return {
        "sample": sample,
        "node_count": len(nodes),
        "flagged_count": len(flagged),
        "consensus_count": len(nodes) - len(flagged),
        "disagreement_count": len(flagged),
        "flagged_node_ids": [n["node_id"] for n in flagged],
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

WORKFLOW_TITLES = {
    "legal": "Vendor Contract Review — Legal Workflow",
    "loan": "Korean Personal Loan Underwriting",
}

COLORS = {
    # color-vision-safe (Wong 2011 ColorBrewer): warm-red vs cool-green pairing
    "flagged_fg":    "#B91C1C",
    "flagged_bg":    "#FEE2E2",
    "consensus_fg":  "#166534",
    "consensus_bg":  "#DCFCE7",
    "header_bg":     "#1E293B",
    "header_fg":     "#F8FAFC",
    "body_bg":       "#F8FAFC",
    "panel_bg":      "#FFFFFF",
    "border":        "#CBD5E1",
    "muted":         "#475569",
    "code_bg":       "#F1F5F9",
    "red_band":      "#FB923C",   # predicted_color RED
    "yellow_band":   "#FACC15",   # predicted_color YELLOW
    "green_band":    "#22C55E",   # predicted_color GREEN
}


def _color_swatch(predicted: str) -> str:
    bg = {"RED": COLORS["red_band"],
          "YELLOW": COLORS["yellow_band"],
          "GREEN": COLORS["green_band"]}.get(predicted, "#94A3B8")
    # font-size omitted to inherit table.disagreement 17pt cascade
    # (avoids sub-16pt: 0.9em × 17pt = 15.3pt would violate brief)
    return (f'<span style="display:inline-block;padding:3px 12px;border-radius:4px;'
            f'background:{bg};color:#1E293B;font-weight:600;">'
            f'{html.escape(predicted)}</span>')


def _delta_cell(delta: float, flagged: bool) -> str:
    sign = "+" if delta > 0 else ("−" if delta < 0 else "±")
    val = abs(delta)
    color = COLORS["flagged_fg"] if flagged else COLORS["consensus_fg"]
    icon = "🚩" if flagged else "✓"
    return (f'<span style="color:{color};font-weight:700;font-variant-numeric:tabular-nums;">'
            f'{sign}{val:.2f} {icon}</span>')


def _diff_count_cell(count: int) -> str:
    if count == 0:
        return f'<span style="color:{COLORS["muted"]};">—</span>'
    weight = 600 if count >= 3 else 400
    return f'<span style="font-weight:{weight};font-variant-numeric:tabular-nums;">{count}</span>'


def _render_row(node: dict) -> str:
    nid = node["node_id"]
    fn = node["function"]
    mode = node["ai_mode"]
    pred = node["predicted_color"]
    gemini = float(node["gemini"]["aggregate_risk"])
    claude = compute_claude_implied_risk(node)
    delta = claude_delta(node)
    flagged = claude_flagged(node)
    md = mitigation_diff(node)["diff_count"]
    ed = evidence_diff(node)["diff_count"]

    bg = COLORS["flagged_bg"] if flagged else COLORS["panel_bg"]
    row_class = "flagged" if flagged else "consensus"

    return (
        f'<tr class="{row_class}" style="background:{bg};">'
        f'<td><a href="#detail-{html.escape(nid)}" class="node-anchor">{html.escape(nid)}</a></td>'
        f'<td>{html.escape(fn)}</td>'
        f'<td><span style="color:{COLORS["muted"]};">{html.escape(mode)}</span></td>'
        f'<td>{_color_swatch(pred)}</td>'
        f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{gemini:.2f}</td>'
        f'<td style="text-align:right;font-variant-numeric:tabular-nums;">{claude:.2f}</td>'
        f'<td style="text-align:right;">{_delta_cell(delta, flagged)}</td>'
        f'<td style="text-align:right;">{_diff_count_cell(md)}</td>'
        f'<td style="text-align:right;">{_diff_count_cell(ed)}</td>'
        f'</tr>'
    )


def _render_detail_card(node: dict) -> str:
    nid = node["node_id"]
    flagged = claude_flagged(node)
    if not flagged:
        return ""

    gemini = float(node["gemini"]["aggregate_risk"])
    claude = compute_claude_implied_risk(node)
    delta = claude_delta(node)
    md = mitigation_diff(node)
    ed = evidence_diff(node)
    axis = node["claude"]["axis_scores"]
    pconf = peer_confidence(node)

    def _set_block(label: str, items: list[str], color: str) -> str:
        if not items:
            body = f'<span style="color:{COLORS["muted"]};">(none)</span>'
        else:
            chips = "".join(
                f'<code style="display:inline-block;background:{COLORS["code_bg"]};'
                f'padding:4px 10px;margin:3px 4px 3px 0;border-radius:4px;'
                f'font-size:0.95em;border-left:3px solid {color};">{html.escape(i)}</code>'
                for i in items
            )
            body = chips
        return (f'<div style="margin-top:6px;">'
                f'<strong style="color:{color};">{html.escape(label)}</strong>: {body}'
                f'</div>')

    flag_list = "".join(
        f'<li style="margin:6px 0;">{html.escape(f)}</li>'
        for f in node["claude"]["disagreement_flags"]
    ) or '<li style="color:{};">(none)</li>'.format(COLORS["muted"])

    return f'''
<section id="detail-{html.escape(nid)}" class="detail-card" style="
    background:{COLORS["panel_bg"]};
    border:1px solid {COLORS["border"]};
    border-left:6px solid {COLORS["flagged_fg"]};
    border-radius:8px;
    padding:20px 24px;
    margin:18px 0;
    box-shadow:0 1px 3px rgba(0,0,0,0.06);">

  <header style="display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;">
    <h3 style="margin:0;font-size:1.4em;color:{COLORS["flagged_fg"]};">
      🚩 {html.escape(nid)} — {html.escape(node["function"])}
    </h3>
    <span style="color:{COLORS["muted"]};font-size:0.95em;">
      Gemini primary <strong>{gemini:.2f}</strong> · Gemini critic (implied) <strong>{claude:.2f}</strong> ·
      Δ <strong style="color:{COLORS["flagged_fg"]};">{delta:+.2f}</strong> ·
      critic_confidence <strong>{pconf:.2f}</strong>
    </span>
  </header>

  <div style="margin-top:14px;color:{COLORS["muted"]};font-size:0.95em;">
    Gemini critic axis scores (0–5): alignment <strong>{axis["alignment"]}</strong> ·
    coverage <strong>{axis["coverage"]}</strong> ·
    hallucination_risk <strong>{axis["hallucination_risk"]}</strong>
  </div>

  <div style="margin-top:16px;line-height:1.55;">
    <strong>Alternative view (Gemini critic):</strong>
    <p style="margin:6px 0 0 0;">{html.escape(node["claude"]["alternative_view"])}</p>
  </div>

  <div style="margin-top:16px;">
    <strong>Disagreement flags ({len(node["claude"]["disagreement_flags"])}):</strong>
    <ul style="margin:6px 0 0 0;padding-left:24px;">{flag_list}</ul>
  </div>

  <div style="margin-top:18px;padding-top:14px;border-top:1px dashed {COLORS["border"]};">
    <div style="font-weight:700;color:{COLORS["header_bg"]};margin-bottom:6px;">
      Mitigation diff ({md["diff_count"]} differ, {md["union_count"]} union)
    </div>
    {_set_block("Gemini-only", md["gemini_only"], "#3B82F6")}
    {_set_block("Gemini critic-only", md["claude_only"], COLORS["flagged_fg"])}
  </div>

  <div style="margin-top:18px;padding-top:14px;border-top:1px dashed {COLORS["border"]};">
    <div style="font-weight:700;color:{COLORS["header_bg"]};margin-bottom:6px;">
      Evidence (AIID) diff ({ed["diff_count"]} differ, {ed["union_count"]} union)
    </div>
    {_set_block("Gemini-only", ed["gemini_only"], "#3B82F6")}
    {_set_block("Gemini critic-only", ed["claude_only"], COLORS["flagged_fg"])}
  </div>
</section>
'''


def render_html(sample: str) -> str:
    nodes = FIXTURE[sample]
    summary = viz_workflow_summary(sample)
    title = WORKFLOW_TITLES.get(sample, sample.title())

    # Sort: flagged first (by |Δ| desc), then consensus (by node_id asc)
    def sort_key(n):
        d = abs(claude_delta(n))
        return (-1 if claude_flagged(n) else 1, -d, n["node_id"])

    rows_sorted = sorted(nodes, key=sort_key)

    rows_html = "\n".join(_render_row(n) for n in rows_sorted)
    detail_cards = "\n".join(_render_detail_card(n) for n in rows_sorted if claude_flagged(n))

    # Summary badges (top hero)
    flagged_badge = (
        f'<span style="background:{COLORS["flagged_fg"]};color:white;padding:8px 16px;'
        f'border-radius:6px;font-weight:700;font-size:1.1em;letter-spacing:0.02em;">'
        f'🚩 {summary["flagged_count"]} nodes flagged</span>'
    )
    consensus_badge = (
        f'<span style="background:{COLORS["consensus_fg"]};color:white;padding:8px 16px;'
        f'border-radius:6px;font-weight:700;font-size:1.1em;letter-spacing:0.02em;">'
        f'✓ {summary["consensus_count"]} nodes consensus</span>'
    )
    phoenix_badge = (
        f'<span style="background:{COLORS["header_bg"]};color:{COLORS["header_fg"]};'
        f'padding:8px 16px;border-radius:6px;font-family:ui-monospace,Menlo,monospace;'
        f'font-size:1.0em;">'
        f'fde.peer.disagreement_count = {summary["disagreement_count"]}</span>'
    )

    flagged_id_chips = " ".join(
        f'<code style="background:{COLORS["code_bg"]};padding:3px 9px;border-radius:4px;'
        f'border-left:3px solid {COLORS["flagged_fg"]};font-size:0.95em;">{nid}</code>'
        for nid in summary["flagged_node_ids"]
    ) if summary["flagged_node_ids"] else '<em>(none — full consensus)</em>'

    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Gemini Critic Disagreement — {html.escape(title)}</title>
  <meta name="viewport" content="width=1920, initial-scale=1">
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      background: {COLORS["body_bg"]};
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
                   "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
      /* ≥16pt enforced (brief: 1920×1080 video capture clarity). 18pt base → all child em ≥0.89 stays ≥16pt. */
      font-size: 18pt;
      color: #0F172A;
      line-height: 1.5;
    }}
    .frame {{
      max-width: 1820px;
      margin: 0 auto;
      padding: 40px 50px;
      min-height: 1000px;
    }}
    header.hero {{
      background: linear-gradient(135deg, #1E293B 0%, #334155 100%);
      color: white;
      padding: 32px 36px;
      border-radius: 12px;
      margin-bottom: 28px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.10);
    }}
    header.hero h1 {{
      margin: 0;
      font-size: 2.0em;
      letter-spacing: -0.01em;
    }}
    header.hero .sub {{
      margin-top: 10px;
      font-size: 1.05em;
      color: #CBD5E1;
      max-width: 1100px;
    }}
    header.hero .badges {{
      margin-top: 22px;
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
    }}
    header.hero .flagged-anchors {{
      margin-top: 16px;
      font-size: 0.95em;
      color: #CBD5E1;
    }}
    table.disagreement {{
      width: 100%;
      border-collapse: collapse;
      background: {COLORS["panel_bg"]};
      box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      border-radius: 8px;
      overflow: hidden;
      font-size: 17pt;   /* ≥16pt enforced; row text 17pt = 22.66px */
    }}
    table.disagreement th {{
      background: {COLORS["header_bg"]};
      color: {COLORS["header_fg"]};
      text-align: left;
      padding: 14px 16px;
      font-weight: 600;
      font-size: 0.95em;
      letter-spacing: 0.02em;
    }}
    table.disagreement th.num {{ text-align: right; }}
    table.disagreement td {{
      padding: 14px 16px;
      border-top: 1px solid {COLORS["border"]};
      vertical-align: middle;
    }}
    table.disagreement tr.flagged td:first-child {{
      border-left: 4px solid {COLORS["flagged_fg"]};
    }}
    table.disagreement tr.consensus td:first-child {{
      border-left: 4px solid {COLORS["consensus_fg"]};
    }}
    a.node-anchor {{
      color: {COLORS["header_bg"]};
      font-weight: 700;
      text-decoration: none;
      border-bottom: 1px dotted {COLORS["muted"]};
    }}
    a.node-anchor:hover {{ color: {COLORS["flagged_fg"]}; }}
    section.tables-wrap {{ margin-bottom: 30px; }}
    h2.section {{
      font-size: 1.5em;
      margin: 30px 0 16px 0;
      color: {COLORS["header_bg"]};
    }}
    footer.legend {{
      margin-top: 36px;
      padding: 20px 24px;
      background: {COLORS["panel_bg"]};
      border: 1px solid {COLORS["border"]};
      border-radius: 8px;
      font-size: 0.95em;
      color: {COLORS["muted"]};
      line-height: 1.7;
    }}
    footer.legend code {{
      background: {COLORS["code_bg"]};
      padding: 2px 6px;
      border-radius: 3px;
      font-size: 0.95em;
    }}
  </style>
</head>
<body>
<div class="frame">

  <header class="hero">
    <h1>Gemini Critic Disagreement — {html.escape(title)}</h1>
    <div class="sub">
      <em>We diagnose AI workflows — our own diagnosis is self-diagnosed.</em>
      Sub-Agent 2 (Gemini primary) vs Sub-Agent 6 (Gemini self-critic, same model family,
      adversarial 2nd-pass) per-node cross-check. |Δ| &gt; {DELTA_FLAG_THRESHOLD} on the [0, 5] risk scale flags
      self-consistency disagreement (Phoenix <code>fde.peer.alert = True</code>).
    </div>
    <div class="badges">
      {flagged_badge}
      {consensus_badge}
      {phoenix_badge}
    </div>
    <div class="flagged-anchors">
      Flagged nodes: {flagged_id_chips}
    </div>
  </header>

  <section class="tables-wrap">
    <h2 class="section">Per-node disagreement table (sorted: flagged first by |Δ|)</h2>
    <table class="disagreement">
      <thead>
        <tr>
          <th>Node</th>
          <th>Function</th>
          <th>AI mode</th>
          <th>Predicted</th>
          <th class="num">Gemini score</th>
          <th class="num">Gemini critic score (implied)</th>
          <th class="num">Δ (G − C)</th>
          <th class="num">Mitigation diff</th>
          <th class="num">Evidence diff</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </section>

  <section class="cards-wrap">
    <h2 class="section">Flagged-node details (alternative view + diff sets)</h2>
    {detail_cards if detail_cards else f'<p style="color:{COLORS["muted"]};font-style:italic;">No flagged nodes — full consensus across workflow.</p>'}
  </section>

  <footer class="legend">
    <strong>Δ semantics:</strong> Δ = Gemini primary.aggregate_risk − Gemini critic.implied_risk (signed, [-5, +5]).
    <code>Δ &gt; 0</code> means the critic thinks primary <em>over</em>-scored; <code>Δ &lt; 0</code> means the critic thinks primary <em>under</em>-scored.<br>
    <strong>Gemini critic implied risk derivation:</strong> critic axis_scores measure confidence-in-primary (orthogonal to risk magnitude); viz layer derives implied risk on the same [0, 5] scale from <code>alternative_view</code> text patterns + flag count. Deterministic. See module docstring.<br>
    <strong>Phoenix 5-metric overlay:</strong> per-node <code>fde.peer.delta</code>, <code>fde.peer.flagged</code>; crew-root <code>fde.peer.disagreement_count</code>. Schema: <code>scripts/observability/phoenix/dashboard_config.json §peer_review</code>.<br>
    <strong>Color palette:</strong> Wong-2011 ColorBrewer pairing (warm-red <code>{COLORS["flagged_fg"]}</code> / cool-green <code>{COLORS["consensus_fg"]}</code>) — color-vision-friendly for deuteranopia &amp; protanopia.<br>
    <strong>Source:</strong> <code>scripts/heatmap/peer_review_disagreement.py</code> ←
    <code>scripts/heatmap/_peer_review_fixture.py</code> (BRAIN_PEER=mock fixture; Gemini key 도착 후 real wire).
  </footer>

</div>
</body>
</html>
'''


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def write_html(sample: str, output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"peer-review-disagreement-{sample}-v0.1.html"
    out_path.write_text(render_html(sample), encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("sample", nargs="?", default="both",
                    choices=("legal", "loan", "both"),
                    help="workflow to render (default: both)")
    args = ap.parse_args()

    samples = ["legal", "loan"] if args.sample == "both" else [args.sample]
    print("=== peer-review disagreement heatmap (Wave 2) ===")
    for s in samples:
        path = write_html(s)
        summary = viz_workflow_summary(s)
        kb = path.stat().st_size / 1024
        flagged_str = ", ".join(summary["flagged_node_ids"]) or "(none)"
        print(f"  [{s:5s}] wrote {path.relative_to(REPO_SCRIPTS)} ({kb:.1f} KB)")
        print(f"          {summary['node_count']} nodes · "
              f"flagged={summary['flagged_count']} ({flagged_str}) · "
              f"consensus={summary['consensus_count']} · "
              f"disagreement_count={summary['disagreement_count']}")

    # Invariants (anchor-based, matches peer_review_prompt.md calibration anchors)
    legal_summary = viz_workflow_summary("legal")
    loan_summary = viz_workflow_summary("loan")
    # Anchor 2: legal N3 must be flagged (borderline RED)
    assert "N3" in legal_summary["flagged_node_ids"], \
        f"legal N3 must be flagged (peer_review_prompt anchor 2), got {legal_summary['flagged_node_ids']}"
    # Anchor 1: loan N7 must be consensus (concur with primary)
    assert "N7" not in loan_summary["flagged_node_ids"], \
        f"loan N7 must be consensus (peer_review_prompt anchor 1), got flagged={loan_summary['flagged_node_ids']}"
    # All GREEN nodes (concur, no flags) must be consensus
    for sample in ("legal", "loan"):
        for n in FIXTURE[sample]:
            if n["predicted_color"] == "GREEN" and not n["claude"]["disagreement_flags"]:
                assert not claude_flagged(n), \
                    f"{sample} {n['node_id']} GREEN+concur must be consensus, Δ={claude_delta(n)}"
    # |Δ| > 0.5 threshold consistent
    for sample in ("legal", "loan"):
        s = viz_workflow_summary(sample)
        for nid in s["flagged_node_ids"]:
            n = next(x for x in FIXTURE[sample] if x["node_id"] == nid)
            assert abs(claude_delta(n)) > DELTA_FLAG_THRESHOLD, \
                f"{sample} {nid} listed flagged but |Δ|={abs(claude_delta(n))}"

    print(f"\n✅ invariants pass (n={sum(viz_workflow_summary(s)['node_count'] for s in ['legal','loan'])} nodes; "
          f"flagged={sum(viz_workflow_summary(s)['flagged_count'] for s in ['legal','loan'])}).")


if __name__ == "__main__":
    main()
