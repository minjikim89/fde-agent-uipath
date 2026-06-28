"""
FDE Agent — Mermaid flowchart Parser → TopologicalGraph

This module parses Mermaid `flowchart TD` blocks using regex + a lightweight AST.
Accepts ```mermaid blocks from legal/loan sample BPMN markdown as direct input.

Supported node shapes (used in samples):
  - `N1[/"..."/]`            trapezoid (process task) — standard for this sample
  - `N1["..."]`              rectangle
  - `D1{...}`                diamond (decision gateway)
  - `Start([...])`           stadium / event
Supported edges:
  - `A --> B`
  - `A -- "label" --> B`
  - `A -- label --> B`
Supported styles:
  - `style Nxx fill:#ffcccc`   → RED
  - `style Nxx fill:#ffe6cc`   → YELLOW
  - `style Nxx fill:#ccffcc`   → GREEN (HITL marker)

Color → diagnosis color mapping:
  #ffcccc → RED, #ffe6cc → YELLOW, #ccffcc → GREEN

Key exports:
  - parse_mermaid(mermaid_text: str, sample_id: str) -> TopologicalGraph
  - parse_markdown_with_mermaid(md_text: str, sample_id: str) -> TopologicalGraph
"""
from __future__ import annotations

import re
from pathlib import Path

from .bpmn import Node, Edge, TopologicalGraph, infer_category_from_label


# Color hex → diagnosis color
COLOR_HEX_TO_DX = {
    "#ffcccc": "RED",
    "#ffe6cc": "YELLOW",
    "#ccffcc": "GREEN",  # HITL marker in our convention
}


# Node definition regex (Mermaid shape patterns)
# trapezoid: N1[/"... content ..."/]
RE_NODE_TRAPEZOID = re.compile(r'^\s*([A-Za-z_][\w]*)\[/"(.+?)"/\]\s*$', re.DOTALL)
# rectangle: N1["..."]  or  N1[...]
RE_NODE_RECT = re.compile(r'^\s*([A-Za-z_][\w]*)\["?(.+?)"?\]\s*$', re.DOTALL)
# diamond / decision: D1{...}
RE_NODE_DIAMOND = re.compile(r'^\s*([A-Za-z_][\w]*)\{(.+?)\}\s*$', re.DOTALL)
# stadium / event: Start([...])
RE_NODE_STADIUM = re.compile(r'^\s*([A-Za-z_][\w]*)\(\[(.+?)\]\)\s*$', re.DOTALL)


# edge regex — with / without label
RE_EDGE_LABELED = re.compile(
    r'^\s*([A-Za-z_][\w]*)\s*--\s*"?(.+?)"?\s*-->\s*([A-Za-z_][\w]*)\s*$'
)
RE_EDGE_PLAIN = re.compile(
    r'^\s*([A-Za-z_][\w]*)\s*-->\s*([A-Za-z_][\w]*)\s*$'
)

# style
RE_STYLE = re.compile(r'^\s*style\s+([A-Za-z_][\w]*)\s+fill\s*:\s*(#[0-9A-Fa-f]{6})')

# Inline node + edge: single-line patterns like `Start([...]) --> N1`
RE_INLINE_STADIUM_EDGE = re.compile(
    r'^\s*([A-Za-z_][\w]*)\(\[(.+?)\]\)\s*-->\s*([A-Za-z_][\w]*)\s*$', re.DOTALL
)


def _extract_node_attrs(label_html: str) -> dict:
    """
    Mermaid node labels may contain inline HTML such as `<br/>` and `<i>...</i>`.
    "N1: Intake & Classify<br/><i>AI: full automation</i><br/>type = NDA / MSA"
    → {"function": "Intake & Classify", "ai_mode": "full automation", "rest": "type = NDA / MSA"}
    """
    raw = label_html
    raw_clean = re.sub(r"<i>(.*?)</i>", r"\1", raw, flags=re.DOTALL)
    parts = [p.strip() for p in re.split(r"<br\s*/?>", raw_clean) if p.strip()]
    function = parts[0] if parts else label_html
    # First line is typically "N1: function name"
    m = re.match(r'^([A-Za-z_]\w*)[\s:]+(.+)$', function)
    if m:
        function = m.group(2).strip()
    ai_mode = ""
    rest = []
    for p in parts[1:]:
        if p.lower().startswith("ai:"):
            ai_mode = p.split(":", 1)[1].strip()
        elif "hitl" in p.lower() or "action center" in p.lower():
            ai_mode = "HITL"
            rest.append(p)
        elif p.lower().startswith("rpa") or "rpa" in p.lower()[:5]:
            ai_mode = "RPA"
            rest.append(p)
        else:
            rest.append(p)
    return {
        "function": function,
        "ai_mode": ai_mode or "untouched",
        "rest": " | ".join(rest),
        "raw_label": label_html,
    }


def parse_mermaid(mermaid_text: str, sample_id: str = "mermaid") -> TopologicalGraph:
    """
    Mermaid flowchart text → TopologicalGraph.
    Parses nodes, edges, and styles in order; also supports multi-line node definitions
    (Mermaid expects one line per definition, but this function is permissive).
    """
    graph = TopologicalGraph(
        sample_id=sample_id,
        metadata={"source_format": "mermaid"},
    )
    # Prevent duplicate node_id registration
    seen_node_ids: set[str] = set()

    def add_node(nid: str, label_html: str, shape: str):
        if nid in seen_node_ids:
            return
        attrs = _extract_node_attrs(label_html)
        category = infer_category_from_label(attrs["function"] + " " + attrs["rest"], attrs["ai_mode"])
        # Force shape for decision gateways
        if shape == "diamond":
            category = "decision"
        elif shape == "stadium":
            # Start/End event — typically skipped as graph boundary marker,
            # but the parser preserves it and marks it in metadata
            pass
        node = Node(
            id=nid,
            label=attrs["function"],
            category=category,
            ai_mode=attrs["ai_mode"],
            color="GREEN",  # default; overridden by style line
            metadata={
                "mermaid_shape": shape,
                "raw_label": attrs["raw_label"],
                "rest": attrs["rest"],
            },
        )
        graph.nodes.append(node)
        seen_node_ids.add(nid)

    # Pass 1: process line by line
    lines = mermaid_text.split("\n")
    # Skip the first Mermaid line `flowchart TD`
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("%%"):
            continue  # comment
        if line.strip().lower().startswith("flowchart"):
            continue
        if line.strip().lower().startswith("classdef"):
            continue
        if line.strip().lower().startswith("linkstyle"):
            continue

        # inline stadium + edge (e.g., `Start([..]) --> N1`)
        m = RE_INLINE_STADIUM_EDGE.match(line)
        if m:
            src, lbl, tgt = m.groups()
            add_node(src, lbl, "stadium")
            # The target will be registered when its definition appears later
            if tgt not in seen_node_ids:
                # Not yet seen — no placeholder created (registered when its definition appears)
                pass
            graph.edges.append(Edge(src=src, tgt=tgt))
            continue

        # node defs
        for shape, regex in (
            ("trapezoid", RE_NODE_TRAPEZOID),
            ("stadium", RE_NODE_STADIUM),
            ("diamond", RE_NODE_DIAMOND),
        ):
            m = regex.match(line)
            if m:
                nid, lbl = m.groups()
                add_node(nid, lbl, shape)
                break
        else:
            # rect is ambiguous with the above shapes (`N1["..."]`)
            # only attempted if diamond·trapezoid·stadium all failed to match
            m = RE_NODE_RECT.match(line)
            if m:
                nid, lbl = m.groups()
                add_node(nid, lbl, "rect")
                continue

        # edges
        m = RE_EDGE_LABELED.match(line)
        if m:
            src, lbl, tgt = m.groups()
            graph.edges.append(Edge(src=src, tgt=tgt, label=lbl.strip().strip('"')))
            continue
        m = RE_EDGE_PLAIN.match(line)
        if m:
            src, tgt = m.groups()
            graph.edges.append(Edge(src=src, tgt=tgt))
            continue

        # style
        m = RE_STYLE.match(line)
        if m:
            nid, hex_color = m.groups()
            color = COLOR_HEX_TO_DX.get(hex_color.lower(), "GREEN")
            node = graph.node_by_id(nid)
            if node:
                node.color = color
                node.metadata["fill"] = hex_color
            continue

    # Pass 2: register as stubs any IDs referenced in edges but not defined as nodes
    referenced = set()
    for e in graph.edges:
        referenced.add(e.src)
        referenced.add(e.tgt)
    defined = {n.id for n in graph.nodes}
    for missing in referenced - defined:
        graph.nodes.append(Node(
            id=missing, label=missing, category="tool_call",
            ai_mode="untouched", color="GREEN",
            metadata={"mermaid_shape": "stub", "auto_created": True},
        ))

    # Pass 3: mark loopbacks (optional)
    # Simple topological sort — identify back edges
    order = {n.id: i for i, n in enumerate(graph.nodes)}
    for e in graph.edges:
        if e.src in order and e.tgt in order and order[e.tgt] < order[e.src]:
            e.is_loopback = True

    return graph


def parse_markdown_with_mermaid(md_text: str, sample_id: str = "md") -> TopologicalGraph:
    """
    Extract and parse the ```mermaid block from a BPMN sample markdown (legal/loan).
    Blocking: only the first mermaid block is used.
    """
    m = re.search(r"```mermaid\s*\n(.*?)\n```", md_text, re.DOTALL)
    if not m:
        raise ValueError(f"No ```mermaid block found in {sample_id}")
    mermaid_src = m.group(1)
    graph = parse_mermaid(mermaid_src, sample_id=sample_id)
    graph.metadata["source_format"] = "markdown_with_mermaid"
    graph.metadata["mermaid_raw"] = mermaid_src
    return graph


def parse_markdown_file(path: str | Path) -> TopologicalGraph:
    p = Path(path)
    return parse_markdown_with_mermaid(p.read_text(encoding="utf-8"), sample_id=p.stem)


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else \
        "scripts/data/sample-workflows/legal-contract-review-v0.1.md"
    g = parse_markdown_file(target)
    print(f"sample_id   = {g.sample_id}")
    print(f"nodes       = {len(g.nodes)}")
    print(f"edges       = {len(g.edges)}")
    print(f"categories  = {g.category_counts()}")
    print(f"RED nodes   = {[n.id for n in g.red_nodes()]}")
    print(f"YELLOW      = {[n.id for n in g.nodes if n.color=='YELLOW']}")
    print(f"GREEN(HITL) = {[n.id for n in g.nodes if n.color=='GREEN' and n.metadata.get('fill')=='#ccffcc']}")
    print("\nfirst 3 nodes detail:")
    for n in g.nodes[:3]:
        print(json.dumps({
            "id": n.id, "label": n.label, "category": n.category,
            "ai_mode": n.ai_mode, "color": n.color,
        }, ensure_ascii=False, indent=2))
