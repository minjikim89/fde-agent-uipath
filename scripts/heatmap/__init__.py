"""FDE Agent — Layer 3 renderers (heatmap + executive summary + side-by-side diff)."""
from .render import render_heatmap_html, render_to_file, build_aggregated_nodes
from .executive_summary import (
    grade_workflow,
    top_failure_modes,
    render_executive_summary,
    render_to_file as render_summary_to_file,
)
from .side_by_side import (
    apply_must_fix_to_mermaid,
    render_side_by_side_html,
    render_to_file as render_side_by_side_to_file,
)

__all__ = [
    "render_heatmap_html",
    "render_to_file",
    "build_aggregated_nodes",
    "render_executive_summary",
    "render_summary_to_file",
    "grade_workflow",
    "top_failure_modes",
    "apply_must_fix_to_mermaid",
    "render_side_by_side_html",
    "render_side_by_side_to_file",
]
