"""
Sub-Agent 1 (Gemini brain) — Workflow Parser

입력:
  - markdown_text (mermaid block 포함) — sample 형식
  - image_path  (PNG/PDF BPMN) — Gemini multimodal OCR

출력:
  - TopologicalGraph dict (scripts.parser 와 호환)

Brain 사용:
  - mermaid markdown 입력은 deterministic — brain 호출 없이 직접 parser 사용
  - image 입력은 brain.generate_multimodal() 호출해서 mermaid text 추출 후 parser 위임
  - Mock/not-ready brain일 경우 image OCR은 빈 그래프 return (parser/image_ocr와 동일 contract)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ._base import GeminiSubAgentBase
from ...parser import (
    TopologicalGraph,
    parse_markdown_with_mermaid,
    parse_mermaid,
)


GEMINI_OCR_PROMPT = """\
You are converting a Business Process Diagram image into Mermaid flowchart syntax.

Output ONLY the Mermaid code block content (no surrounding markdown fences, no explanation).
Use this convention:
  - Process nodes:    `N1[/"N1: <function name><br/><i>AI: <mode></i><br/><detail>"/]`
  - Decision diamond: `D1{<question>}`
  - Start/End oval:   `Start([<label>])`, `End([<label>])`
  - Edges:            `A --> B`  or  `A -- "branch label" --> B`
  - Color (if visible): `style N1 fill:#ffcccc` (red), `#ffe6cc` (orange), `#ccffcc` (green)

Begin with `flowchart TD`. Output Mermaid text only.
"""


class SubAgent1ParserGemini(GeminiSubAgentBase):
    name = "sub_agent_1_parser_gemini"

    def parse_markdown(self, md_text: str, sample_id: str = "input") -> TopologicalGraph:
        return parse_markdown_with_mermaid(md_text, sample_id=sample_id)

    def parse_image(self, image_path: str | Path, sample_id: Optional[str] = None) -> TopologicalGraph:
        sid = sample_id or Path(image_path).stem
        if self.is_mock:
            # graceful — caller가 ocr_failed 플래그로 alternate path 가능
            return TopologicalGraph(
                sample_id=sid,
                metadata={
                    "source_format": "image_gemini_ocr",
                    "image_path": str(image_path),
                    "ocr_failed": True,
                    "brain": "mock",
                },
            )
        mermaid_text = self.brain.generate_multimodal(GEMINI_OCR_PROMPT, image_path=str(image_path))
        if not mermaid_text or not mermaid_text.strip():
            return TopologicalGraph(
                sample_id=sid,
                metadata={"source_format": "image_gemini_ocr", "image_path": str(image_path), "ocr_failed": True},
            )
        # fence stripping
        if mermaid_text.startswith("```"):
            mermaid_text = "\n".join(l for l in mermaid_text.splitlines() if not l.startswith("```"))
        graph = parse_mermaid(mermaid_text, sample_id=sid)
        graph.metadata.update({
            "source_format": "image_gemini_ocr",
            "image_path": str(image_path),
            "brain": self.brain.name,
        })
        return graph

    def run(self, md_text: Optional[str] = None, image_path: Optional[str] = None, sample_id: str = "input") -> TopologicalGraph:
        if md_text is not None:
            return self.parse_markdown(md_text, sample_id=sample_id)
        if image_path is not None:
            return self.parse_image(image_path, sample_id=sample_id)
        raise ValueError("Sub-Agent 1: either md_text or image_path required")
