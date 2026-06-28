"""
scripts.metrics — Handoff Quantification Framework metric modules.

The body modules in this package (ips.py / confdecay.py / laaj.py) are read-only.
This __init__.py exposes only the graph_score hook (combined with 🅓 GraphRAG retrieve.py output).

graph_score is aligned with 🅓 retrieve.py's Hit.graph_score: float:
  - dense_score (BGE-M3 cosine) vs graph_score (GraphRAG path traversal)
  - final = 0.5 * dense + 0.5 * graph_score
  - This graph_score hook isolates the graph portion for reuse in handoff diagnosis

This sprint freezes only the graph_score interface — wiring happens in metric_overlay.py after 🅓 merge.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GraphScore:
    """
    Graph component of 🅓 GraphRAG retrieve.py output — 4th metric for Phoenix overlay.

    Schema alignment:
      retrieve.py Hit.graph_score: float          ↔  .score
      retrieve.py path traversal depth (optional) ↔  .path_length
      retrieve.py matched OWASP/MITRE standards   ↔  .matched_standards
      retrieve.py domain filter pass              ↔  .matched_domain
      retrieve.py vertical filter pass            ↔  .matched_vertical
    """
    score: float                                  # 0.0 ~ 1.0
    path_length: int = 0                          # graph traversal depth
    matched_standards: list = field(default_factory=list)
    matched_domain: str = ""
    matched_vertical: str = ""
    provenance: str = ""                          # which source incident provided the boost

    def __post_init__(self):
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"GraphScore.score must be in [0,1]: {self.score}")

    def to_attributes(self, prefix: str = "fde.handoff.graph") -> dict:
        """Returns a flat dict suitable for Phoenix span attributes."""
        attrs = {
            f"{prefix}.score":             round(self.score, 4),
            f"{prefix}.path_length":       int(self.path_length),
            f"{prefix}.matched_standards": list(self.matched_standards),
            f"{prefix}.matched_domain":    self.matched_domain or "",
            f"{prefix}.matched_vertical":  self.matched_vertical or "",
        }
        if self.provenance:
            attrs[f"{prefix}.provenance"] = self.provenance
        return attrs


# Re-exports (for convenient external imports)
__all__ = ["GraphScore"]
