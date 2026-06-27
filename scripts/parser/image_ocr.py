"""
FDE Agent — Image / PDF OCR Parser → TopologicalGraph

전략: Gemini multimodal vision (Vertex AI, `google-genai` SDK + ADC) 으로 이미지·PDF를
Mermaid flowchart text 로 변환 후, scripts/parser/mermaid.py 재활용. Rapid 경로 = Gemini-only
(비-Gemini AI tool 금지 규정 정합). API key 불필요 — Application Default Credentials 만 사용
(Cloud Run runtime service account / local `gcloud auth application-default login`).

본 모듈은 stand-alone 데모용 stub + 실제 호출 양쪽을 지원:
  - run_gemini_ocr(image_path)            실제 Gemini multimodal 호출 (Vertex AI)
  - parse_image(image_path, sample_id)    end-to-end: OCR → mermaid → TopologicalGraph
  - parse_pdf(pdf_path, sample_id)        PDF에 동일 처리
  - mock_ocr_to_mermaid()                 unit test용 dummy (외부 호출 회피)

Gemini SDK 미설치·ADC 미설정·호출 실패 시 빈 그래프 return (e2e dry-run을 막지 않기 위함).
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from .bpmn import TopologicalGraph
from .mermaid import parse_mermaid


# Vertex AI Gemini multimodal model. Rapid rules require Gemini 3 — operators
# override via VERTEX_GEMINI_MODEL / GEMINI_OCR_MODEL env. The default below is
# the project-pinned preview snapshot (gemini-3-pro-preview is discontinued —
# never use it).
DEFAULT_GEMINI_OCR_MODEL = os.environ.get(
    "GEMINI_OCR_MODEL",
    os.environ.get("VERTEX_GEMINI_MODEL", "gemini-3.1-pro-preview"),
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


def _gemini_sdk_available() -> bool:
    """google-genai SDK 설치 여부 (Vertex 라우팅 경로)."""
    try:
        import google.genai  # noqa: F401  # type: ignore
        return True
    except ImportError:
        return False


def _strip_mermaid_fences(text: str) -> str:
    """Gemini 가 ```mermaid ... ``` fence 박을 가능성 — strip."""
    out = text.strip()
    if out.startswith("```"):
        out = "\n".join(l for l in out.split("\n") if not l.startswith("```"))
    return out.strip()


def run_gemini_ocr(image_path: str | Path, timeout: int = 120) -> str:
    """
    Gemini multimodal (Vertex AI) 호출 → Mermaid text.

    SDK 미설치·ADC 미설정·호출 실패 시 빈 string return (caller 가 fallback 결정).
    `timeout` 은 SDK request 옵션으로 전달 (밀리초 단위, google-genai HttpOptions).
    """
    img = Path(image_path)
    if not img.exists():
        raise FileNotFoundError(img)
    if not _gemini_sdk_available():
        return ""
    try:
        from google import genai  # type: ignore
        from google.genai import types  # type: ignore
    except ImportError:
        return ""

    # Route SDK to Vertex AI (ADC, no API key) — idempotent.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

    mime_type, _ = mimetypes.guess_type(str(img))
    if not mime_type:
        # PDF / unknown → sensible defaults; Gemini accepts application/pdf + image/*.
        mime_type = "application/pdf" if img.suffix.lower() == ".pdf" else "image/png"

    try:
        client = genai.Client(
            http_options=types.HttpOptions(api_version="v1", timeout=timeout * 1000),
        )
        image_part = types.Part.from_bytes(data=img.read_bytes(), mime_type=mime_type)
        response = client.models.generate_content(
            model=DEFAULT_GEMINI_OCR_MODEL,
            contents=[GEMINI_OCR_PROMPT, image_part],
        )
    except Exception:
        # ADC 미설정 / quota / transient — graceful degradation.
        return ""

    text = getattr(response, "text", None)
    if not text:
        return ""
    return _strip_mermaid_fences(text)


def mock_ocr_to_mermaid() -> str:
    """test·CI용 — 외부 호출 없이 sample mermaid return."""
    return """flowchart TD
    Start([Image input]) --> N1
    N1[/"N1: OCR'd task<br/><i>AI: full automation</i>"/]
    N1 --> N2
    N2[/"N2: Reviewer step<br/><i>HITL</i>"/]
    N2 --> End([Done])
    style N1 fill:#ffcccc
    style N2 fill:#ccffcc
"""


def parse_image(image_path: str | Path, sample_id: str | None = None, use_mock: bool = False) -> TopologicalGraph:
    sid = sample_id or Path(image_path).stem
    if use_mock:
        mermaid_text = mock_ocr_to_mermaid()
    else:
        mermaid_text = run_gemini_ocr(image_path)
        if not mermaid_text:
            # Gemini OCR 실패 → 빈 그래프 (graceful degradation)
            return TopologicalGraph(
                sample_id=sid,
                metadata={
                    "source_format": "image_ocr",
                    "image_path": str(image_path),
                    "ocr_failed": True,
                },
            )
    graph = parse_mermaid(mermaid_text, sample_id=sid)
    graph.metadata["source_format"] = "image_ocr"
    graph.metadata["image_path"] = str(image_path)
    graph.metadata["ocr_mermaid_raw"] = mermaid_text
    return graph


def parse_pdf(pdf_path: str | Path, sample_id: str | None = None) -> TopologicalGraph:
    """
    PDF 는 Gemini 가 직접 받음 (Vertex AI multimodal 은 application/pdf 입력 지원).
    실패 시 빈 graph + ocr_failed=True.
    """
    return parse_image(pdf_path, sample_id=sample_id)


if __name__ == "__main__":
    # 데모 — mock OCR로 parser 동작만 검증
    g = parse_image("dummy.png", sample_id="mock", use_mock=True)
    print(f"mock OCR → graph: nodes={len(g.nodes)} edges={len(g.edges)} REDs={[n.id for n in g.red_nodes()]}")
