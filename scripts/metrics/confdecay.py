"""
ConfDecay — Confidence Decay (Handoff Quantification Framework, ontology v0.3 §metric_2)

formula:
  ConfDecay(node_a → node_b) = node_b.confidence - node_a.confidence
  Over-Trust Gap  = max(0,  ConfDecay)  when node_a.confidence < 0.8
  Under-Use Gap   = max(0, -ConfDecay)  when node_a.confidence > 0.9

thresholds (ontology spec):
  Over-Trust Gap > 0.2 → ALERT: downstream이 upstream uncertainty 무시
  Under-Use Gap  > 0.2 → WARNING: downstream이 confident upstream을 underuse
  abs(decay) ≤ 0.1     → healthy

requirement: 각 sub-agent output에 confidence score attach 필수

v0.2 — 4-source confidence aggregation (외부 review 채택):
  effective_conf = 0.3 * logprobs_proxy
                 + 0.3 * self_reflection
                 + 0.2 * tool_exec_clarity
                 + 0.2 * (1.0 - timeout_signal)
  단일 logprobs 대신 4-source weighted sum → single-point fragility 회피.
  Phoenix span attribute로 4개 분리 emit 가능 (disagreement 시각화 자료).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


OVER_TRUST_UPSTREAM_MAX = 0.8     # over-trust gap만 적용되는 upstream conf 상한
UNDER_USE_UPSTREAM_MIN = 0.9      # under-use gap만 적용되는 upstream conf 하한
GAP_ALERT_THRESHOLD = 0.2
HEALTHY_DECAY_MAX = 0.1

# 4-source weighted sum (외부 review 채택)
SOURCE_WEIGHTS = {
    'logprobs_proxy':    0.3,    # exp(avg_token_logprob) — LLM 자체 신뢰 신호
    'self_reflection':   0.3,    # "How confident are you? 0~1" 프롬프트 응답
    'tool_exec_clarity': 0.2,    # tool 호출 return code 0 + structured output presence
    'timeout_signal':    0.2,    # 호출 시간 임계 초과 정도 — 1.0=full timeout (분리 적용 시 1-x로 반전)
}


@dataclass
class ConfDecayResult:
    upstream: str
    downstream: str
    upstream_conf: float
    downstream_conf: float
    decay: float
    over_trust_gap: float
    under_use_gap: float
    band: str          # healthy | over_trust_alert | under_use_warning | mild_drift
    alert: bool

    def to_row(self) -> str:
        return (
            f"| {self.upstream} → {self.downstream} "
            f"| {self.upstream_conf:.2f} | {self.downstream_conf:.2f} "
            f"| {self.decay:+.2f} | OT={self.over_trust_gap:.2f} UU={self.under_use_gap:.2f} "
            f"| {self.band} | {'⚠️' if self.alert else '✅'} |"
        )


@dataclass
class ConfidenceSources:
    """4-source confidence inputs per node. 각 source는 [0,1] normalized.
       timeout_signal만 inverse semantics (높을수록 신뢰 낮음 → 1-x 반전 후 가중)."""
    logprobs_proxy: float = 0.0
    self_reflection: float = 0.0
    tool_exec_clarity: float = 0.0
    timeout_signal: float = 0.0       # 0 = no timeout, 1 = full timeout

    def __post_init__(self):
        for k in ('logprobs_proxy', 'self_reflection', 'tool_exec_clarity', 'timeout_signal'):
            v = getattr(self, k)
            if not (0.0 <= v <= 1.0):
                raise ValueError(f"{k} must be in [0,1]: {v}")

    def breakdown(self) -> dict:
        """Phoenix span attribute로 박을 수 있는 dict 형태."""
        return {
            'logprobs_proxy':    self.logprobs_proxy,
            'self_reflection':   self.self_reflection,
            'tool_exec_clarity': self.tool_exec_clarity,
            'timeout_signal':    self.timeout_signal,
        }


def combine_confidence_sources(sources: ConfidenceSources) -> tuple[float, dict]:
    """4-source weighted sum → effective confidence + per-source contribution dict.
       timeout은 (1 - timeout_signal)로 반전 후 가중."""
    contrib = {
        'logprobs_proxy':    SOURCE_WEIGHTS['logprobs_proxy']    * sources.logprobs_proxy,
        'self_reflection':   SOURCE_WEIGHTS['self_reflection']   * sources.self_reflection,
        'tool_exec_clarity': SOURCE_WEIGHTS['tool_exec_clarity'] * sources.tool_exec_clarity,
        'timeout_signal':    SOURCE_WEIGHTS['timeout_signal']    * (1.0 - sources.timeout_signal),
    }
    effective = sum(contrib.values())
    # clamp [0,1]
    effective = max(0.0, min(1.0, effective))
    return effective, contrib


@dataclass
class ConfDecaySourcesResult:
    """compute_confdecay_from_sources 결과 — base ConfDecayResult + 4-source breakdown."""
    base: 'ConfDecayResult'
    upstream_sources: ConfidenceSources
    downstream_sources: ConfidenceSources
    upstream_breakdown: dict
    downstream_breakdown: dict
    source_disagreement: dict          # per-source: abs(up - dn) — 어느 source가 큰 변동 보이는지

    def to_breakdown_row(self) -> str:
        b = self.base
        return (
            f"| {b.upstream} → {b.downstream} | {b.upstream_conf:.2f} | {b.downstream_conf:.2f} "
            f"| Δ={b.decay:+.2f} | {b.band} | {'⚠️' if b.alert else '✅'} "
            f"| top-source-disagreement: {max(self.source_disagreement, key=self.source_disagreement.get)}"
            f"={self.source_disagreement[max(self.source_disagreement, key=self.source_disagreement.get)]:.2f} |"
        )


def compute_confdecay_from_sources(upstream_sources: ConfidenceSources,
                                   downstream_sources: ConfidenceSources,
                                   upstream_label: str,
                                   downstream_label: str) -> ConfDecaySourcesResult:
    """4-source 입력 → effective conf weighted sum → 기존 compute_confdecay 위임.
       Phoenix attribute로 박을 수 있는 per-source breakdown 동시 반환."""
    up_eff, up_contrib = combine_confidence_sources(upstream_sources)
    dn_eff, dn_contrib = combine_confidence_sources(downstream_sources)
    base = compute_confdecay(up_eff, dn_eff, upstream_label, downstream_label)

    up_b = upstream_sources.breakdown()
    dn_b = downstream_sources.breakdown()
    disagreement = {k: abs(up_b[k] - dn_b[k]) for k in up_b}

    return ConfDecaySourcesResult(
        base=base,
        upstream_sources=upstream_sources,
        downstream_sources=downstream_sources,
        upstream_breakdown=up_b,
        downstream_breakdown=dn_b,
        source_disagreement=disagreement,
    )


def compute_confdecay(upstream_conf: float,
                      downstream_conf: float,
                      upstream_label: str,
                      downstream_label: str) -> ConfDecayResult:
    if not (0.0 <= upstream_conf <= 1.0 and 0.0 <= downstream_conf <= 1.0):
        raise ValueError(f"confidence must be in [0,1]: up={upstream_conf} dn={downstream_conf}")

    decay = downstream_conf - upstream_conf

    over_trust = max(0.0, decay) if upstream_conf < OVER_TRUST_UPSTREAM_MAX else 0.0
    under_use = max(0.0, -decay) if upstream_conf > UNDER_USE_UPSTREAM_MIN else 0.0

    if over_trust > GAP_ALERT_THRESHOLD:
        band = "over_trust_alert"
        alert = True
    elif under_use > GAP_ALERT_THRESHOLD:
        band = "under_use_warning"
        alert = True
    elif abs(decay) <= HEALTHY_DECAY_MAX:
        band = "healthy"
        alert = False
    else:
        band = "mild_drift"
        alert = False

    return ConfDecayResult(
        upstream=upstream_label,
        downstream=downstream_label,
        upstream_conf=upstream_conf,
        downstream_conf=downstream_conf,
        decay=decay,
        over_trust_gap=over_trust,
        under_use_gap=under_use,
        band=band,
        alert=alert,
    )


# -----------------------------------------------------------------------------
# unit test — ontology의 loan_N6→N7 핵심 사례 (low LLM conf → high auto-decision conf)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # case 1: ontology spec의 silent escalation 시그니처
    #         N6 LLM 위험 분석 0.70 → N7 자동 결정 0.99 = over-trust
    r1 = compute_confdecay(0.70, 0.99, "loan_N6", "loan_N7")
    assert r1.band == "over_trust_alert", r1
    assert r1.alert
    assert abs(r1.over_trust_gap - 0.29) < 1e-6

    # case 2: healthy propagation
    r2 = compute_confdecay(0.85, 0.88, "N2", "N3")
    assert r2.band == "healthy", r2
    assert not r2.alert

    # case 3: under-use — confident upstream을 downstream이 underuse
    r3 = compute_confdecay(0.95, 0.60, "N3", "N5b")
    assert r3.band == "under_use_warning", r3
    assert r3.alert
    assert abs(r3.under_use_gap - 0.35) < 1e-6

    # case 4: mild drift — alert는 아니지만 not healthy
    r4 = compute_confdecay(0.70, 0.85, "N4", "N5a")
    assert r4.band == "mild_drift", r4
    assert not r4.alert

    # case 5: invalid input
    try:
        compute_confdecay(1.5, 0.5, "X", "Y")
        assert False, "should have raised"
    except ValueError:
        pass

    # ----- v0.2 4-source 케이스 -----
    print()
    print("--- 4-source confidence aggregation ---")

    # case A: 모든 source healthy (0.9 effective)
    src_healthy = ConfidenceSources(
        logprobs_proxy=0.90, self_reflection=0.90,
        tool_exec_clarity=0.95, timeout_signal=0.05)
    eff_a, contrib_a = combine_confidence_sources(src_healthy)
    # 0.3*0.9 + 0.3*0.9 + 0.2*0.95 + 0.2*(1-0.05) = 0.27+0.27+0.19+0.19 = 0.92
    assert 0.85 <= eff_a <= 0.95, eff_a
    print(f"  case A — all healthy:      effective={eff_a:.3f} contrib={ {k: round(v,3) for k,v in contrib_a.items()} }")

    # case B: logprobs 낮은데 self-reflection은 over-confident — single source fragility 시연
    src_disagree = ConfidenceSources(
        logprobs_proxy=0.50, self_reflection=0.95,
        tool_exec_clarity=0.80, timeout_signal=0.0)
    eff_b, contrib_b = combine_confidence_sources(src_disagree)
    # 0.15 + 0.285 + 0.16 + 0.2 = 0.795 — single logprobs 0.50보다 robust한 0.79
    assert 0.70 <= eff_b <= 0.85, eff_b
    print(f"  case B — logprobs vs self-reflection 불일치: effective={eff_b:.3f}  "
          f"(logprobs 0.50 단일 source보다 +{eff_b-0.50:.2f} robust)")

    # case C: tool execution 실패 — clarity 0.0
    src_tool_fail = ConfidenceSources(
        logprobs_proxy=0.85, self_reflection=0.85,
        tool_exec_clarity=0.00, timeout_signal=0.05)
    eff_c, _ = combine_confidence_sources(src_tool_fail)
    # 0.255 + 0.255 + 0.0 + 0.19 = 0.70
    assert eff_c < 0.75, eff_c
    print(f"  case C — tool 실패 (clarity=0): effective={eff_c:.3f}")

    # case D: timeout 발생 — timeout_signal=1.0
    src_timeout = ConfidenceSources(
        logprobs_proxy=0.85, self_reflection=0.85,
        tool_exec_clarity=0.85, timeout_signal=1.0)
    eff_d, _ = combine_confidence_sources(src_timeout)
    # 0.255 + 0.255 + 0.17 + 0.0 = 0.68
    assert eff_d < 0.75, eff_d
    print(f"  case D — timeout (signal=1.0): effective={eff_d:.3f}")

    # case E: loan_N6 → loan_N7 silent escalation w/ 4-source — over_trust_alert 재현
    up_src = ConfidenceSources(
        logprobs_proxy=0.65, self_reflection=0.72,
        tool_exec_clarity=0.80, timeout_signal=0.15)
    dn_src = ConfidenceSources(
        logprobs_proxy=0.99, self_reflection=0.99,
        tool_exec_clarity=1.00, timeout_signal=0.0)
    r_e = compute_confdecay_from_sources(up_src, dn_src, "loan_N6", "loan_N7")
    assert r_e.base.band == "over_trust_alert", r_e.base
    assert r_e.base.alert
    top_dis = max(r_e.source_disagreement, key=r_e.source_disagreement.get)
    print(f"  case E — loan_N6→loan_N7 silent escalation (4-source):")
    print(f"    effective_up={r_e.base.upstream_conf:.2f} effective_dn={r_e.base.downstream_conf:.2f} "
          f"decay={r_e.base.decay:+.2f} band={r_e.base.band} alert={r_e.base.alert}")
    print(f"    top-source-disagreement: {top_dis}={r_e.source_disagreement[top_dis]:.2f}")
    print(f"    upstream breakdown:   {r_e.upstream_breakdown}")
    print(f"    downstream breakdown: {r_e.downstream_breakdown}")

    # case F: invalid source value
    try:
        ConfidenceSources(logprobs_proxy=1.5, self_reflection=0.5, tool_exec_clarity=0.5, timeout_signal=0.0)
        assert False, "should have raised"
    except ValueError:
        pass

    print("confdecay.py unit tests passed:")
    for r in [r1, r2, r3, r4]:
        print(" ", r.to_row())
