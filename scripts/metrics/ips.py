"""
IPS — Intent Preservation Score (Handoff Quantification Framework, ontology v0.3 §metric_1)

formula:
  IPS(node_a → node_b) = cosine_similarity(
    BGE-M3.embed(node_a.output_text),
    BGE-M3.embed(node_b.diagnosis_or_summary_text)
  )

thresholds (ontology spec):
  >= 0.7  healthy handoff
  0.5~0.7 watch zone
  <  0.5  Context Decay Alert (silent failure risk)

infra: BGE-M3 인프라 그대로 사용 (diagnose.py와 동일 model 재사용 가능)

v0.2 — embed backend factory (deterministic production):
  - bge_m3_embed_fn()       : sentence-transformers BAAI/bge-m3 production (deterministic, semantic)
  - fallback_hash_embed_fn(): pure-python hash embed (venv 부재 시 fallback, PYTHONHASHSEED 의존)
  - get_embed_fn(prefer)    : "bge-m3" 시도 → 실패 시 fallback 자동 swap + backend label 반환
unit test는 fake_embed로 deterministic invariant 유지.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

ALERT_THRESHOLD = 0.7
DECAY_ALERT_HARD = 0.5


@dataclass
class IPSResult:
    upstream: str
    downstream: str
    score: float
    band: str           # healthy | watch | decay_alert
    alert: bool

    def to_row(self) -> str:
        return f"| {self.upstream} → {self.downstream} | {self.score:.3f} | {self.band} | {'⚠️' if self.alert else '✅'} |"


# -----------------------------------------------------------------------------
# Embed backend factories (v0.2)
# -----------------------------------------------------------------------------

def bge_m3_embed_fn(model=None, device: Optional[str] = None) -> Callable[[str], list]:
    """
    Production BGE-M3 embedder. sentence-transformers BAAI/bge-m3 normalize_embeddings=True.
    Lazy load — venv 없으면 ImportError 발생 → get_embed_fn에서 fallback swap.
    """
    if model is None:
        from sentence_transformers import SentenceTransformer
        try:
            import torch
            dev = device or ('mps' if torch.backends.mps.is_available() else 'cpu')
        except ImportError:
            dev = device or 'cpu'
        model = SentenceTransformer('BAAI/bge-m3', device=dev)

    def _embed(text: str) -> list:
        return model.encode([text], normalize_embeddings=True).tolist()[0]
    return _embed


def fallback_hash_embed_fn(dim: int = 256) -> Callable[[str], list]:
    """
    Pure-python deterministic-given-PYTHONHASHSEED hash embed.
    Production aware: PYTHONHASHSEED 미고정 시 실행마다 결과 다름 — venv 부재 시만 사용.
    """
    import math, re
    def _embed(text: str) -> list:
        vec = [0.0] * dim
        for tok in re.findall(r'\w+', text.lower()):
            for shift in range(2):
                vec[hash(tok + str(shift)) % dim] += 1.0
        n = math.sqrt(sum(v * v for v in vec)) or 1e-12
        return [v / n for v in vec]
    return _embed


def get_embed_fn(prefer: str = "bge-m3") -> Tuple[Callable[[str], list], str]:
    """
    Production-aware factory — BGE-M3 시도 후 venv 부재 시 fallback hash로 자동 swap.
    Returns: (embed_fn, backend_label)  backend_label ∈ {"bge-m3", "hash-fallback"}
    """
    if prefer == "bge-m3":
        try:
            return bge_m3_embed_fn(), "bge-m3"
        except Exception:
            return fallback_hash_embed_fn(), "hash-fallback"
    if prefer == "hash":
        return fallback_hash_embed_fn(), "hash-fallback"
    raise ValueError(f"unknown prefer: {prefer}")


def _cosine(a, b) -> float:
    # pure python — numpy 의존성 회피 (diagnose.py 환경에 한정)
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(y * y for y in b)) or 1e-12
    return dot / (na * nb)


def compute_ips(upstream_text: str,
                downstream_text: str,
                upstream_label: str,
                downstream_label: str,
                embed_fn) -> IPSResult:
    """
    embed_fn: callable(text: str) -> list[float]
              (BGE-M3 wrapper — diagnose.py의 model.encode 재사용)
    """
    if not upstream_text or not downstream_text:
        return IPSResult(upstream_label, downstream_label, 0.0, "decay_alert", True)

    emb_up = embed_fn(upstream_text)
    emb_dn = embed_fn(downstream_text)
    score = _cosine(emb_up, emb_dn)

    if score >= ALERT_THRESHOLD:
        band = "healthy"
        alert = False
    elif score >= DECAY_ALERT_HARD:
        band = "watch"
        alert = False
    else:
        band = "decay_alert"
        alert = True

    return IPSResult(upstream_label, downstream_label, score, band, alert)


# -----------------------------------------------------------------------------
# unit test (ontology spec sanity)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import math

    def fake_embed(text: str):
        # 결정적 fake embedding — 단어 집합 hash 기반 vector
        vec = [0.0] * 32
        for tok in text.lower().split():
            vec[hash(tok) % 32] += 1.0
        return vec

    # case 1: identical text → IPS ≈ 1.0 (healthy)
    r1 = compute_ips("clause indemnity liability", "clause indemnity liability",
                     "N2", "N3", fake_embed)
    assert r1.band == "healthy", r1
    assert math.isclose(r1.score, 1.0, abs_tol=1e-6), r1.score
    assert not r1.alert

    # case 2: partial overlap → watch zone
    r2 = compute_ips("clause indemnity liability terms warranty schedule",
                     "indemnity payment audit unrelated random noise",
                     "N2", "N3", fake_embed)
    assert 0.0 < r2.score < 0.9, r2

    # case 3: no overlap → decay_alert
    r3 = compute_ips("alpha beta gamma", "x y z", "N2", "N3", fake_embed)
    assert r3.band == "decay_alert"
    assert r3.alert

    # case 4: empty → alert
    r4 = compute_ips("", "x", "N2", "N3", fake_embed)
    assert r4.alert
    assert r4.score == 0.0

    # ----- v0.2: get_embed_fn factory sanity (BGE-M3 시도 후 자동 fallback) -----
    embed_fn_auto, backend = get_embed_fn(prefer="bge-m3")
    print(f"\n--- get_embed_fn factory: backend selected = {backend!r} ---")
    # backend는 venv 환경에 따라 "bge-m3" or "hash-fallback" 둘 중 하나
    assert backend in ("bge-m3", "hash-fallback"), backend

    # production-equivalent invariant: identical input → IPS ≈ 1.0
    sample_text = "clause indemnity liability termination"
    r_id = compute_ips(sample_text, sample_text, "N2", "N3", embed_fn_auto)
    assert r_id.score >= 0.95, f"identical text should produce IPS ≈ 1.0, got {r_id.score} (backend={backend})"
    assert r_id.band == "healthy"
    print(f"  identical-text invariant: IPS={r_id.score:.3f} band={r_id.band} ✅")

    print("ips.py unit tests passed:")
    for r in [r1, r2, r3, r4]:
        print(" ", r.to_row())
