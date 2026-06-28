"""
FDE Agent — BPMN 2.0 XML Parser → TopologicalGraph

This module normalizes BPMN 2.0 XML input into a TopologicalGraph dataclass.
Classifies into 8 node types (classification / generation / RAG / tool_call / human_review / handoff / decision / external_send).
Designed for compatibility with the node_type field in ontology v0.3c (mapping-ontology-v0.1.yaml).

Key exports:
  - Node, Edge, TopologicalGraph (dataclass)
  - NODE_CATEGORIES (8-category definitions)
  - NodeCategory enum-like Literal
  - categorize_from_ontology_type(ontology_node_type: str) -> str
  - parse_bpmn_xml(xml_string: str) -> TopologicalGraph
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


NODE_CATEGORIES = (
    "classification",   # classification — LLM/ML classifier, OCR, name matching, scoring
    "generation",       # generation — LLM free-text generation (letter, draft, summary)
    "rag",              # RAG — vector retrieval over external corpus
    "tool_call",        # tool_call — external API, deterministic tool, function call
    "human_review",     # human_review — HITL (Action Center, attorney review, manual underwriting)
    "handoff",          # handoff — explicit transfer/edge node (rare as node, mostly as edge attribute)
    "decision",         # decision — auto-decision engine, gateway with criteria
    "external_send",    # external_send — outbound communication (email, e-sign, rejection letter dispatch)
)


def categorize_from_ontology_type(ontology_node_type: str) -> str:
    """Normalize an ontology node_type into one of this module's 8 categories."""
    if not ontology_node_type:
        return "tool_call"
    t = ontology_node_type.lower()
    if "classification" in t or "ocr" in t or "name_matching" in t or "scoring" in t:
        return "classification"
    if "generation" in t:
        return "generation"
    if "rag" in t or "retrieval" in t:
        return "rag"
    if "decision" in t:
        return "decision"
    if "hitl" in t or "human" in t or "review" in t:
        return "human_review"
    if "external" in t or "send" in t or "communication" in t:
        return "external_send"
    if "comparison" in t:
        return "classification"  # comparison/scoring → classification family
    return "tool_call"


def infer_category_from_label(label: str, ai_mode: str) -> str:
    """
    Mermaid and image inputs lack ontology node_type metadata,
    so the category is inferred heuristically from label + ai_mode.
    """
    txt = f"{label} {ai_mode}".lower()
    if "hitl" in txt or "action center" in txt or "attorney" in txt or "underwriter" in txt or "human" in txt:
        return "human_review"
    if "rpa" in txt and ("send" in txt or "email" in txt or "notification" in txt):
        return "external_send"
    if "e-signature" in txt or "agreement" in txt or "remittance" in txt or "sign-off" in txt:
        return "external_send"
    if "audit" in txt or "log" in txt or "rpa" in txt:
        return "tool_call"
    if "rejection" in txt or "letter" in txt or "draft" in txt or "counterproposal" in txt or "generation" in txt:
        return "generation"
    if "decision" in txt or "review decision" in txt or "auto-approve" in txt or "approve" in txt:
        return "decision"
    if "rag" in txt or "retrieve" in txt or "search" in txt:
        return "rag"
    if "api" in txt or "tool" in txt or "redirect" in txt:
        return "tool_call"
    if any(k in txt for k in ("classify", "ocr", "identity verification", "extract", "scoring", "evaluation", "risk flagging", "screening")):
        return "classification"
    return "tool_call"


@dataclass
class Node:
    id: str                         # e.g. "N2" / "loan_N6"
    label: str                      # human-readable function
    category: str                   # one of NODE_CATEGORIES
    ai_mode: str = "untouched"      # full_automation / decision_support / HITL / RPA / untouched
    color: str = "GREEN"            # RED / YELLOW / GREEN
    confidence: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Edge:
    src: str
    tgt: str
    label: Optional[str] = None     # decision branch label (e.g., "Yes (score < 0.3)")
    is_loopback: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class TopologicalGraph:
    sample_id: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def node_by_id(self, nid: str) -> Optional[Node]:
        for n in self.nodes:
            if n.id == nid:
                return n
        return None

    def red_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.color == "RED"]

    def red_yellow_nodes(self) -> list[Node]:
        return [n for n in self.nodes if n.color in ("RED", "YELLOW")]

    def edges_from(self, nid: str) -> list[Edge]:
        return [e for e in self.edges if e.src == nid]

    def edges_to(self, nid: str) -> list[Edge]:
        return [e for e in self.edges if e.tgt == nid]

    def to_dict(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
            "metadata": self.metadata,
        }

    def category_counts(self) -> dict[str, int]:
        out = {c: 0 for c in NODE_CATEGORIES}
        for n in self.nodes:
            out[n.category] = out.get(n.category, 0) + 1
        return out


# BPMN 2.0 XML namespaces (multiple included for compatibility)
BPMN_NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmn2": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "bpmndi": "http://www.omg.org/spec/BPMN/20100524/DI",
}


# BPMN element → 8-category mapping
BPMN_ELEMENT_CATEGORY = {
    "task": "tool_call",
    "userTask": "human_review",
    "manualTask": "human_review",
    "serviceTask": "tool_call",
    "scriptTask": "tool_call",
    "businessRuleTask": "decision",
    "sendTask": "external_send",
    "receiveTask": "tool_call",
    "subProcess": "tool_call",
    "exclusiveGateway": "decision",
    "inclusiveGateway": "decision",
    "parallelGateway": "handoff",
    "complexGateway": "decision",
}


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_bpmn_xml(xml_string: str, sample_id: str = "bpmn") -> TopologicalGraph:
    """
    BPMN 2.0 XML → TopologicalGraph.

    Maps node types directly to 8 categories. Color information is extracted from BPMN extensions
    ("RED"/"YELLOW"/"GREEN" keywords inside `<bpmn:documentation>`).
    """
    root = ET.fromstring(xml_string)
    graph = TopologicalGraph(sample_id=sample_id, metadata={"source_format": "bpmn_xml"})

    process = None
    for elem in root.iter():
        if _strip_ns(elem.tag) == "process":
            process = elem
            break

    if process is None:
        process = root

    for child in process:
        tag = _strip_ns(child.tag)
        nid = child.attrib.get("id")
        if not nid:
            continue

        if tag in BPMN_ELEMENT_CATEGORY:
            label = child.attrib.get("name", nid)
            category = BPMN_ELEMENT_CATEGORY[tag]
            doc_text = ""
            for sub in child.iter():
                if _strip_ns(sub.tag) == "documentation" and sub.text:
                    doc_text = sub.text
                    break
            color = "GREEN"
            if "RED" in doc_text.upper():
                color = "RED"
            elif "YELLOW" in doc_text.upper():
                color = "YELLOW"
            ai_mode = "full_automation" if tag in ("serviceTask", "scriptTask", "businessRuleTask") else \
                      "HITL" if tag in ("userTask", "manualTask") else \
                      "RPA" if tag == "sendTask" else "untouched"
            graph.nodes.append(Node(
                id=nid, label=label, category=category, ai_mode=ai_mode, color=color,
                metadata={"bpmn_element": tag},
            ))
        elif tag == "sequenceFlow":
            graph.edges.append(Edge(
                src=child.attrib.get("sourceRef", ""),
                tgt=child.attrib.get("targetRef", ""),
                label=child.attrib.get("name") or None,
            ))
        elif tag in ("startEvent", "endEvent"):
            # events are treated as graph boundary markers only (outside the 8 categories)
            pass

    return graph


def parse_bpmn_file(path: str | Path, sample_id: Optional[str] = None) -> TopologicalGraph:
    p = Path(path)
    sid = sample_id or p.stem
    text = p.read_text(encoding="utf-8")
    return parse_bpmn_xml(text, sample_id=sid)


if __name__ == "__main__":
    # When run standalone, prints NODE_CATEGORIES + sample categorize results.
    print("NODE_CATEGORIES =", NODE_CATEGORIES)
    sample_ontology_types = [
        "llm_generation_structured",
        "llm_classification_binary",
        "decision_automation",
        "llm_classification_multimodal",
        "llm_generation_freetext",
        "llm_classification_multiclass",
        "llm_comparison_scoring",
        "llm_augmented_ocr_biometric",
        "rule_plus_llm_name_matching",
        "api_plus_ml_scoring",
    ]
    print("\nontology node_type → category:")
    for t in sample_ontology_types:
        print(f"  {t:42s} → {categorize_from_ontology_type(t)}")
