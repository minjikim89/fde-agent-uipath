"""
LaaJ — LLM-as-a-Judge (Handoff Quantification Framework, ontology v0.3 §metric_3)

formula:
  LaaJ(node_a, node_b) = JudgeLLM.score(
    prompt=judge_prompt.md + JSON(context, node_a.output, node_b.output),
    judge_model="claude (Sonnet, UiPath) | mock"
  )
  → returns alignment_score [0,1], axis_scores, reasoning, disagreement_flags

thresholds (ontology spec):
  alignment_score < 0.6        → manual review trigger
  disagreement_flags non-empty → specific issue review
  >= 0.8                       → trusted

cost note: judge LLM call per handoff = $$ → sampling (10% spot-check) 권장.
구독 우선 원칙: `claude -p` subprocess 1순위, Anthropic SDK는 fallback/명시 요청 시.
"""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

JUDGE_PROMPT_PATH = Path(__file__).parent.parent / "agents" / "judge_prompt.md"

MANUAL_REVIEW_THRESHOLD = 0.6
TRUSTED_THRESHOLD = 0.8

_log = logging.getLogger(__name__)


@dataclass
class LaaJResult:
    upstream: str
    downstream: str
    alignment_score: float
    axis_scores: dict
    reasoning: str
    disagreement_flags: list = field(default_factory=list)
    band: str = ""
    alert: bool = False
    raw_judge_text: str = ""
    judge_backend: str = ""    # "claude_cli" | "mock" | "anthropic_sdk"

    def to_row(self) -> str:
        flags = '; '.join(self.disagreement_flags) or '(none)'
        return (
            f"| {self.upstream} → {self.downstream} | {self.alignment_score:.2f} "
            f"| {self.band} | {'⚠️' if self.alert else '✅'} | {flags[:80]} |"
        )


def _read_judge_prompt() -> str:
    return JUDGE_PROMPT_PATH.read_text(encoding="utf-8")


def _build_input_block(context: dict, node_a: dict, node_b: dict) -> str:
    payload = {"context": context, "node_a": node_a, "node_b": node_b}
    return "Input:\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def _interpret(alignment_score: float, disagreement_flags: list) -> tuple[str, bool]:
    if alignment_score < MANUAL_REVIEW_THRESHOLD:
        return "manual_review_trigger", True
    if disagreement_flags:
        return "specific_issue", True
    if alignment_score >= TRUSTED_THRESHOLD:
        return "trusted", False
    return "mid", False


def _extract_json(text: str) -> dict:
    """Claude output에서 첫 JSON object 추출 (markdown code fence 또는 raw)."""
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in judge output: {text[:300]}")
    return json.loads(text[start:end + 1])


# -----------------------------------------------------------------------------
# Judge backends
# -----------------------------------------------------------------------------

def _judge_via_claude_cli(prompt: str, model: Optional[str] = None) -> str:
    if shutil.which("claude") is None:
        raise RuntimeError("`claude` CLI not on PATH")
    cmd = ["claude", "-p", prompt]
    if model:
        cmd = ["claude", "-p", "--model", model, prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {proc.stderr[:500]}")
    return proc.stdout


def _judge_via_mock(context: dict, node_a: dict, node_b: dict) -> dict:
    """결정적 mock — unit test + offline demo용. 단어 overlap 휴리스틱으로 점수."""
    a_tokens = set(str(node_a.get("output", "")).lower().split())
    b_tokens = set(str(node_b.get("output", "")).lower().split())
    overlap = len(a_tokens & b_tokens)
    ref_size = max(1, len(a_tokens))
    align = min(5, int(round(5 * overlap / ref_size)))
    expected_schema = context.get("expected_schema", "")
    schema_hit = 5 if expected_schema and any(k in str(node_b.get("output", "")) for k in expected_schema.split()) else max(1, align - 1)
    axis = {
        "alignment": align,
        "coherence": max(1, align - 1) if align < 5 else 5,
        "factual_consistency": align,
        "schema_preservation": schema_hit,
    }
    score = round(sum(axis.values()) / 4 / 5, 2)
    flags = []
    dropped = a_tokens - b_tokens
    # surface dropped salient tokens (length > 3)
    salient = [t for t in dropped if len(t) > 4][:3]
    if salient:
        flags.append(f"node_b drops upstream tokens: {', '.join(salient)}")
    return {
        "alignment_score": score,
        "axis_scores": axis,
        "reasoning": f"mock judge: token overlap {overlap}/{ref_size}, schema hint match={schema_hit}",
        "disagreement_flags": flags,
    }


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def compute_laaj(context: dict,
                 node_a: dict,
                 node_b: dict,
                 backend: str = "auto",
                 model: Optional[str] = None) -> LaaJResult:
    """
    backend:
      - "auto":       claude CLI 시도 → 실패 시 mock으로 fallback
      - "claude_cli": `claude -p` subprocess 강제
      - "mock":       offline deterministic
    """
    upstream_label = node_a.get("id", "?")
    downstream_label = node_b.get("id", "?")

    use_backend = backend
    judge_text = ""
    if backend in ("auto", "claude_cli"):
        try:
            prompt = _read_judge_prompt() + "\n\n" + _build_input_block(context, node_a, node_b)
            judge_text = _judge_via_claude_cli(prompt, model=model)
            parsed = _extract_json(judge_text)
            use_backend = "claude_cli"
        except Exception as e:
            if backend == "claude_cli":
                raise
            parsed = _judge_via_mock(context, node_a, node_b)
            use_backend = "mock"
            judge_text = f"(claude CLI unavailable: {e}) → mock fallback"
    else:
        parsed = _judge_via_mock(context, node_a, node_b)
        use_backend = "mock"

    score = float(parsed.get("alignment_score", 0.0))
    band, alert = _interpret(score, parsed.get("disagreement_flags") or [])

    return LaaJResult(
        upstream=upstream_label,
        downstream=downstream_label,
        alignment_score=score,
        axis_scores=parsed.get("axis_scores", {}),
        reasoning=parsed.get("reasoning", ""),
        disagreement_flags=parsed.get("disagreement_flags", []),
        band=band,
        alert=alert,
        raw_judge_text=judge_text,
        judge_backend=use_backend,
    )


# -----------------------------------------------------------------------------
# unit test (mock backend only, no LLM call required)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # case 1: well-aligned handoff → trusted (no flags)
    ctx = {
        "workflow": "legal",
        "handoff_pair": "N2 → N3",
        "ontology_handoff_risk": "schema_contract_drift",
        "expected_schema": "clauses risk_vector",
    }
    r1 = compute_laaj(
        ctx,
        node_a={"id": "N2", "type": "extractor",
                "output": "clauses indemnity liability termination warranty audit"},
        node_b={"id": "N3", "type": "LLM_risk",
                "output": "clauses indemnity liability termination warranty risk_vector flagged"},
        backend="mock",
    )
    assert r1.judge_backend == "mock"
    assert r1.alignment_score >= 0.6, r1

    # case 2: broken handoff (no overlap, no schema) → manual review trigger
    r2 = compute_laaj(
        ctx,
        node_a={"id": "N2", "type": "extractor",
                "output": "indemnity liability schedule warranty"},
        node_b={"id": "N3", "type": "LLM_risk",
                "output": "alpha bravo charlie"},
        backend="mock",
    )
    assert r2.alignment_score < 0.6, r2
    assert r2.alert
    assert r2.band == "manual_review_trigger"

    # case 3: ontology의 loan_N6→N7 silent escalation 시그니처
    ctx_loan = {
        "workflow": "korean_loan",
        "handoff_pair": "loan_N6 → loan_N7",
        "ontology_handoff_risk": "false_negative_cascade",
        "expected_schema": "fraud_score reasoning confidence",
    }
    r3 = compute_laaj(
        ctx_loan,
        node_a={"id": "loan_N6", "type": "LLM_risk",
                "output": "fraud_score 0.4 confidence 0.70 reasoning: 이상 신호 약함, 보이스피싱 victim 패턴 가능성 잔존"},
        node_b={"id": "loan_N7", "type": "auto_decision",
                "output": "approved confidence 0.99"},
        backend="mock",
    )
    assert r3.alert or r3.alignment_score < 0.8, r3   # 적어도 trusted는 아님

    # case 4: explicit mock backend, raw text fallback message
    assert r1.raw_judge_text == "" or "mock" in r1.raw_judge_text.lower() or r1.judge_backend == "mock"

    print("laaj.py unit tests passed:")
    for r in [r1, r2, r3]:
        print(" ", r.to_row())
