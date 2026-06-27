"""FDE Agent — Workflow parsers (BPMN XML / Mermaid / Image OCR)."""
from .bpmn import (
    Node,
    Edge,
    TopologicalGraph,
    NODE_CATEGORIES,
    categorize_from_ontology_type,
    infer_category_from_label,
    parse_bpmn_xml,
    parse_bpmn_file,
)
from .mermaid import parse_mermaid, parse_markdown_with_mermaid, parse_markdown_file
from .image_ocr import parse_image, parse_pdf, mock_ocr_to_mermaid

__all__ = [
    "Node", "Edge", "TopologicalGraph", "NODE_CATEGORIES",
    "categorize_from_ontology_type", "infer_category_from_label",
    "parse_bpmn_xml", "parse_bpmn_file",
    "parse_mermaid", "parse_markdown_with_mermaid", "parse_markdown_file",
    "parse_image", "parse_pdf", "mock_ocr_to_mermaid",
]
