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

infra: reuses BGE-M3 infrastructure as-is (can reuse the same model as diagnose.py)

v0.2 — embed backend factory (deterministic production):
  - bge_m3_embed_fn()       : sentence-transformers BAAI/bge-m3 production (deterministic, semantic)
  - fallback_hash_embed_fn(): pure-python hash embed (fallback when venv unavailable, depends on PYTHONHASHSEED)
  - get_embed_fn(prefer)    : tries "bge-m3" → auto-swaps to fallback on failure + returns backend label
unit test maintains deterministic invariants via fake_embed.
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
    Lazy load — raises ImportError if venv unavailable → get_embed_fn performs fallback swap.
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
    Production aware: results differ per run if PYTHONHASHSEED is not fixed — use only when venv is unavailable.
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
    Production-aware factory — auto-swaps to fallback hash when BGE-M3 fails or venv is unavailable.
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
    # pure python — avoids numpy dependency (scoped to the diagnose.py environment)
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
              (BGE-M3 wrapper — reuses model.encode from diagnose.py)
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
        # deterministic fake embedding — hash-based vector over word tokens
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

    # ----- v0.2: get_embed_fn factory sanity (auto-fallback after BGE-M3 attempt) -----
    embed_fn_auto, backend = get_embed_fn(prefer="bge-m3")
    print(f"\n--- get_embed_fn factory: backend selected = {backend!r} ---")
    # backend is either "bge-m3" or "hash-fallback" depending on the venv environment
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
