"""
FDE Agent — Image / PDF OCR Parser → TopologicalGraph

This open-source build ships a deterministic mock OCR (no external vision model).
Image / PDF inputs are converted to a sample Mermaid flowchart, then reused via
scripts/parser/mermaid.py. The public demo runs on BPMN / Mermaid inputs; the
image path is kept import-compatible as a structural placeholder.

  - parse_image(image_path, sample_id)    end-to-end: mock OCR -> mermaid -> TopologicalGraph
  - parse_pdf(pdf_path, sample_id)         same handling for PDFs
  - mock_ocr_to_mermaid()                  deterministic sample (no external call)
"""
from __future__ import annotations

from pathlib import Path

from .bpmn import TopologicalGraph
from .mermaid import parse_mermaid


def _strip_mermaid_fences(text: str) -> str:
    """Strip ``` fences if a renderer wrapped the Mermaid text."""
    out = text.strip()
    if out.startswith("```"):
        out = "\n".join(l for l in out.split("\n") if not l.startswith("```"))
    return out.strip()


def mock_ocr_to_mermaid() -> str:
    """Deterministic sample Mermaid for tests / CI (no external call)."""
    return """flowchart TD
    Start([Image input]) --> N1
    N1[/"N1: OCR'd task<br/><i>AI: full automation</i>"/]
    N1 --> N2
    N2[/"N2: Reviewer step<br/><i>HITL</i>"/]
    N2 --> End([Done])
    style N1 fill:#ffcccc
    style N2 fill:#ccffcc
"""


def parse_image(image_path: str | Path, sample_id: str | None = None, use_mock: bool = True) -> TopologicalGraph:
    """Image -> TopologicalGraph. This build uses the deterministic mock OCR."""
    sid = sample_id or Path(image_path).stem
    mermaid_text = mock_ocr_to_mermaid()
    graph = parse_mermaid(mermaid_text, sample_id=sid)
    graph.metadata["source_format"] = "image_ocr"
    graph.metadata["image_path"] = str(image_path)
    graph.metadata["ocr_mermaid_raw"] = mermaid_text
    return graph


def parse_pdf(pdf_path: str | Path, sample_id: str | None = None) -> TopologicalGraph:
    """PDF -> TopologicalGraph (same deterministic mock-OCR handling)."""
    return parse_image(pdf_path, sample_id=sample_id)


if __name__ == "__main__":
    g = parse_image("dummy.png", sample_id="mock", use_mock=True)
    print(f"mock OCR -> graph: nodes={len(g.nodes)} edges={len(g.edges)} REDs={[n.id for n in g.red_nodes()]}")
