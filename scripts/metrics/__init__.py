"""
scripts.metrics — Handoff Quantification Framework metric modules.

본 패키지의 본문 모듈 (ips.py / confdecay.py / laaj.py)은 read-only.
이 __init__.py는 graph_score 후크 (🅓 GraphRAG retrieve.py 산출과 결합) 만 노출.

graph_score는 🅓 retrieve.py의 Hit.graph_score: float와 정합:
  - dense_score (BGE-M3 cosine) vs graph_score (GraphRAG path traversal)
  - final = 0.5 * dense + 0.5 * graph_score
  - 본 graph_score 후크는 그 graph 부분만 isolation해서 handoff 진단에 reuse

본 sprint는 graph_score 인터페이스만 freeze — wire는 🅓 합류 후 metric_overlay.py에서 호출.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GraphScore:
    """
    🅓 GraphRAG retrieve.py 산출의 graph component — Phoenix overlay 4번째 metric.

    Schema 정합:
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
    provenance: str = ""                          # 어느 source incident에서 boost 받았는지

    def __post_init__(self):
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"GraphScore.score must be in [0,1]: {self.score}")

    def to_attributes(self, prefix: str = "fde.handoff.graph") -> dict:
        """Phoenix span attribute로 박을 수 있는 flat dict 반환."""
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


# Re-exports (외부 import 편의)
__all__ = ["GraphScore"]
