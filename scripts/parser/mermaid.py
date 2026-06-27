"""
FDE Agent — Mermaid flowchart Parser → TopologicalGraph

본 모듈은 Mermaid `flowchart TD` 블록을 정규식 + 간단한 AST로 파싱한다.
legal/loan sample BPMN markdown의 ```mermaid 블록을 직접 입력으로 받음.

지원 노드 셰이프 (sample에서 사용되는 것):
  - `N1[/"..."/]`            trapezoid (process task) — 본 sample 표준
  - `N1["..."]`              rectangle
  - `D1{...}`                diamond (decision gateway)
  - `Start([...])`           stadium / event
지원 edge:
  - `A --> B`
  - `A -- "label" --> B`
  - `A -- label --> B`
지원 style:
  - `style Nxx fill:#ffcccc`   → RED
  - `style Nxx fill:#ffe6cc`   → YELLOW
  - `style Nxx fill:#ccffcc`   → GREEN (HITL 표시)

색상 → diagnosis color 매핑:
  #ffcccc → RED, #ffe6cc → YELLOW, #ccffcc → GREEN

핵심 export:
  - parse_mermaid(mermaid_text: str, sample_id: str) -> TopologicalGraph
  - parse_markdown_with_mermaid(md_text: str, sample_id: str) -> TopologicalGraph
"""
from __future__ import annotations

import re
from pathlib import Path

from .bpmn import Node, Edge, TopologicalGraph, infer_category_from_label


# 색상 hex → diagnosis color
COLOR_HEX_TO_DX = {
    "#ffcccc": "RED",
    "#ffe6cc": "YELLOW",
    "#ccffcc": "GREEN",  # HITL marker in our convention
}


# 노드 정의 regex (Mermaid 셰이프 패턴)
# trapezoid: N1[/"... 내용 ..."/]
RE_NODE_TRAPEZOID = re.compile(r'^\s*([A-Za-z_][\w]*)\[/"(.+?)"/\]\s*$', re.DOTALL)
# rectangle: N1["..."]  또는  N1[...]
RE_NODE_RECT = re.compile(r'^\s*([A-Za-z_][\w]*)\["?(.+?)"?\]\s*$', re.DOTALL)
# diamond / decision: D1{...}
RE_NODE_DIAMOND = re.compile(r'^\s*([A-Za-z_][\w]*)\{(.+?)\}\s*$', re.DOTALL)
# stadium / event: Start([...])
RE_NODE_STADIUM = re.compile(r'^\s*([A-Za-z_][\w]*)\(\[(.+?)\]\)\s*$', re.DOTALL)


# edge regex — label 있음 / 없음
RE_EDGE_LABELED = re.compile(
    r'^\s*([A-Za-z_][\w]*)\s*--\s*"?(.+?)"?\s*-->\s*([A-Za-z_][\w]*)\s*$'
)
RE_EDGE_PLAIN = re.compile(
    r'^\s*([A-Za-z_][\w]*)\s*-->\s*([A-Za-z_][\w]*)\s*$'
)

# style
RE_STYLE = re.compile(r'^\s*style\s+([A-Za-z_][\w]*)\s+fill\s*:\s*(#[0-9A-Fa-f]{6})')

# inline node + edge: `Start([...]) --> N1` 같은 단일 라인 처리용
RE_INLINE_STADIUM_EDGE = re.compile(
    r'^\s*([A-Za-z_][\w]*)\(\[(.+?)\]\)\s*-->\s*([A-Za-z_][\w]*)\s*$', re.DOTALL
)


def _extract_node_attrs(label_html: str) -> dict:
    """
    Mermaid 노드 label은 `<br/>`, `<i>...</i>` 등 inline html을 포함.
    "N1: Intake & Classify<br/><i>AI: full automation</i><br/>type = NDA / MSA"
    → {"function": "Intake & Classify", "ai_mode": "full automation", "rest": "type = NDA / MSA"}
    """
    raw = label_html
    raw_clean = re.sub(r"<i>(.*?)</i>", r"\1", raw, flags=re.DOTALL)
    parts = [p.strip() for p in re.split(r"<br\s*/?>", raw_clean) if p.strip()]
    function = parts[0] if parts else label_html
    # 첫 줄은 통상 "N1: function name"
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
    노드·엣지·style을 순서대로 파싱하며 multi-line 노드 정의도 지원
    (Mermaid는 한 line이 원칙이지만 본 함수는 너그럽게 처리).
    """
    graph = TopologicalGraph(
        sample_id=sample_id,
        metadata={"source_format": "mermaid"},
    )
    # node_id 중복 등록 방지
    seen_node_ids: set[str] = set()

    def add_node(nid: str, label_html: str, shape: str):
        if nid in seen_node_ids:
            return
        attrs = _extract_node_attrs(label_html)
        category = infer_category_from_label(attrs["function"] + " " + attrs["rest"], attrs["ai_mode"])
        # decision gateway는 shape 강제
        if shape == "diamond":
            category = "decision"
        elif shape == "stadium":
            # Start/End event — graph 외곽이므로 보통 skip 권장이지만
            # parser는 보존하고 metadata로 표시
            pass
        node = Node(
            id=nid,
            label=attrs["function"],
            category=category,
            ai_mode=attrs["ai_mode"],
            color="GREEN",  # default; style 라인에서 overlay
            metadata={
                "mermaid_shape": shape,
                "raw_label": attrs["raw_label"],
                "rest": attrs["rest"],
            },
        )
        graph.nodes.append(node)
        seen_node_ids.add(nid)

    # 1차 패스: 라인별 처리
    lines = mermaid_text.split("\n")
    # Mermaid 첫 줄 `flowchart TD` skip
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
            # 그 다음 tgt는 추후 등장 시 등록되지만, 아직 모르면 빈 stub
            if tgt not in seen_node_ids:
                # 아직 본 적 없음 — placeholder는 만들지 않음 (이후 정의 시 등록)
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
            # rect는 위 셰이프와 모호 (`N1["..."]`)
            # diamond·trapezoid·stadium에 안 잡혔을 때만 시도
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

    # 2차 패스: edge에서 등장했지만 node로 정의 안 된 id를 stub로 등록
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

    # 3차 패스: loopback 표식 (선택)
    # 단순 topological sort — back edge 식별
    order = {n.id: i for i, n in enumerate(graph.nodes)}
    for e in graph.edges:
        if e.src in order and e.tgt in order and order[e.tgt] < order[e.src]:
            e.is_loopback = True

    return graph


def parse_markdown_with_mermaid(md_text: str, sample_id: str = "md") -> TopologicalGraph:
    """
    BPMN sample markdown (legal/loan)에서 ```mermaid 블록 추출 후 parse.
    blocking: 첫 mermaid block만 사용.
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
