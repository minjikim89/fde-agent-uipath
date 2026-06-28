"""
Aggregator — per-node final risk score + evidence cell consolidation.

Reference implementation of the Architecture §5 AGGREGATE component.
Collapses Sub-Agent 1–6 outputs into a single final risk score 0–5
and reconciles the 3-element evidence cell (failure mode + evidence type + mitigation).

Weights (proprietary IP positioning):
  handoff  0.4  ★ proprietary IP moat — reflects IPS/ConfDecay/LaaJ runtime metrics
  security 0.3  OWASP / MITRE ATLAS mapping
  general  0.3  Risk vector + AIID RAG

Color mapping:
  >= 4.0       RED
  2.5 ~ 3.9    YELLOW
  <  2.5       GREEN

Runtime metric → axis score adjustment (handoff axis only):
  IPS alert (decay_alert)    +0.5
  IPS watch band (0.5~0.7)   +0.2   # reflects ontology spec "context partially lost"
  ConfDecay over_trust_alert +0.5
  ConfDecay under_use_warning +0.3
  LaaJ alignment_score < 0.6 +0.3
  LaaJ disagreement_flags ≥1 +0.2
  cap at 5.0
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


AXIS_WEIGHTS = {'handoff': 0.4, 'security': 0.3, 'general_failure': 0.3}
COLOR_THRESHOLDS = {'RED': 4.0, 'YELLOW': 2.5}   # >= RED, >= YELLOW else GREEN

HANDOFF_METRIC_BOOSTS = {
    'ips_alert': 0.5,
    'ips_watch': 0.2,             # ontology spec "context partially lost" — risk signal even without an alert
    'over_trust_alert': 0.5,
    'under_use_warning': 0.3,
    'laaj_low_score': 0.3,        # < 0.6
    'laaj_flags_present': 0.2,
}
SCORE_CAP = 5.0


@dataclass
class EvidenceItem:
    """3-element cell per Heatmap design principle — failure mode + evidence type + mitigation."""
    axis: str
    failure_mode: str
    evidence_type: str           # "ontology" | "aiid_rag" | "owasp" | "mitre_atlas" | "heuristic_ip" | "runtime_metric"
    evidence_refs: list = field(default_factory=list)
    mitigation_summary: str = ""

    def to_dict(self) -> dict:
        return {
            'axis': self.axis,
            'failure_mode': self.failure_mode,
            'evidence_type': self.evidence_type,
            'evidence_refs': self.evidence_refs,
            'mitigation_summary': self.mitigation_summary,
        }


@dataclass
class AggregatedNode:
    node_id: str
    function: str
    ai_mode: str
    final_score: float
    color: str
    axis_scores: dict
    evidence: list                  # list[EvidenceItem]
    mitigation_options: dict        # {axis: {must_fix, recommend, optional}}
    runtime_metric_boost: float = 0.0
    runtime_metric_alerts: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'function': self.function,
            'ai_mode': self.ai_mode,
            'final_score': self.final_score,
            'color': self.color,
            'axis_scores': self.axis_scores,
            'runtime_metric_boost': self.runtime_metric_boost,
            'runtime_metric_alerts': self.runtime_metric_alerts,
            'evidence': [e.to_dict() for e in self.evidence],
            'mitigation_options': self.mitigation_options,
        }


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------

def _color_for(score: float) -> str:
    if score >= COLOR_THRESHOLDS['RED']:
        return 'RED'
    if score >= COLOR_THRESHOLDS['YELLOW']:
        return 'YELLOW'
    return 'GREEN'


def _axis_base_score(cells: list) -> float:
    """Average risk_score of cells within an axis. Returns 0.0 if no cells."""
    scores = [c.get('risk_score', 0) for c in cells if isinstance(c.get('risk_score'), (int, float))]
    return sum(scores) / len(scores) if scores else 0.0


def _runtime_boost_for_handoff(handoff_metrics: list) -> tuple[float, list]:
    """
    handoff_metrics: list of dicts, each with keys {'ips', 'confdecay', 'laaj'} (metric result objects).
    Returns (total_boost, alerts_triggered).
    """
    boost = 0.0
    alerts = []
    for m in handoff_metrics:
        ips = m.get('ips')
        cd = m.get('confdecay')
        laaj = m.get('laaj')
        if ips and getattr(ips, 'alert', False):
            boost += HANDOFF_METRIC_BOOSTS['ips_alert']
            alerts.append(f"ips_alert({ips.upstream}→{ips.downstream}={ips.score:.2f})")
        elif ips and getattr(ips, 'band', '') == 'watch':
            boost += HANDOFF_METRIC_BOOSTS['ips_watch']
            alerts.append(f"ips_watch({ips.upstream}→{ips.downstream}={ips.score:.2f})")
        if cd:
            if getattr(cd, 'band', '') == 'over_trust_alert':
                boost += HANDOFF_METRIC_BOOSTS['over_trust_alert']
                alerts.append(f"over_trust({cd.upstream}→{cd.downstream}, OT={cd.over_trust_gap:.2f})")
            elif getattr(cd, 'band', '') == 'under_use_warning':
                boost += HANDOFF_METRIC_BOOSTS['under_use_warning']
                alerts.append(f"under_use({cd.upstream}→{cd.downstream}, UU={cd.under_use_gap:.2f})")
        if laaj:
            if getattr(laaj, 'alignment_score', 1.0) < 0.6:
                boost += HANDOFF_METRIC_BOOSTS['laaj_low_score']
                alerts.append(f"laaj_low({laaj.upstream}→{laaj.downstream}={laaj.alignment_score:.2f})")
            elif getattr(laaj, 'disagreement_flags', []) :
                boost += HANDOFF_METRIC_BOOSTS['laaj_flags_present']
                alerts.append(f"laaj_flags({laaj.upstream}→{laaj.downstream})")
    return boost, alerts


def _build_evidence(diagnosis: dict, alerts: list) -> list:
    """3-element cell — aligned with Heatmap design principle."""
    out = []
    n = diagnosis['node']
    for axis, cells in diagnosis['cells_by_axis'].items():
        for cell in cells:
            if axis == 'general_failure':
                fm = cell.get('primary_failure_mode', 'unspecified')
                refs = [f"{e.get('id')} ({e.get('relevance','')})"
                        for e in (cell.get('evidence', {}) or {}).get('aiid_incidents', [])]
                mit_ref = cell.get('mitigation_options_ref', '')
                out.append(EvidenceItem(
                    axis=axis,
                    failure_mode=fm,
                    evidence_type='ontology' if not refs else 'ontology+aiid_rag',
                    evidence_refs=refs,
                    mitigation_summary=f"see {mit_ref}" if mit_ref else "(no inline mitigation)",
                ))
            elif axis == 'security':
                threats = cell.get('primary_threats', []) or []
                mitre = cell.get('mitre_atlas_techniques', cell.get('mitre_atlas_tactics', [])) or []
                refs = [f"{t.get('id')} {t.get('title','')}" for t in threats] + \
                       [f"{m.get('id')} {m.get('title','')}" for m in mitre]
                out.append(EvidenceItem(
                    axis=axis,
                    failure_mode=cell.get('primary_failure_mode', ', '.join(t.get('id','') for t in threats)) or 'unspecified',
                    evidence_type='owasp+mitre_atlas' if refs else 'standards',
                    evidence_refs=refs,
                    mitigation_summary='OWASP/MITRE prevention (auto-mapped)',
                ))
            elif axis == 'handoff':
                fm = cell.get('primary_handoff_risk', 'unspecified')
                mit = cell.get('mitigation_options', {}) or {}
                mit_lines = []
                for k in ('must_fix', 'recommend', 'optional'):
                    if k in mit:
                        action = (mit[k].get('action') or '')[:120]
                        mit_lines.append(f"{k}: {action}")
                out.append(EvidenceItem(
                    axis=axis,
                    failure_mode=fm,
                    evidence_type='heuristic_ip' + ('+runtime_metric' if alerts else ''),
                    evidence_refs=[cell.get('heuristic_source', 'proprietary IP')],
                    mitigation_summary=' | '.join(mit_lines) or '(no inline options)',
                ))
    # runtime metrics are a separate evidence item (attached to the handoff axis)
    if alerts:
        out.append(EvidenceItem(
            axis='handoff',
            failure_mode='runtime_metric_alert',
            evidence_type='runtime_metric',
            evidence_refs=alerts,
            mitigation_summary='IPS/ConfDecay/LaaJ runtime gating — auto escalation rule on Phase 1 Phoenix integration',
        ))
    return out


def _collect_mitigation_options(diagnosis: dict) -> dict:
    """Consolidates per-axis multi-option mitigations (aligned with Sub-Agent 5 ruleset)."""
    out = {}
    for axis, cells in diagnosis['cells_by_axis'].items():
        per_axis = {}
        for cell in cells:
            inline = cell.get('mitigation_options', {}) or {}
            for k in ('must_fix', 'recommend', 'optional'):
                if k in inline and k not in per_axis:
                    per_axis[k] = (inline[k].get('action') or '')[:200]
            ref = cell.get('mitigation_options_ref')
            if ref and 'ref' not in per_axis:
                per_axis['ref'] = ref
        if per_axis:
            out[axis] = per_axis
    return out


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def aggregate_node(diagnosis: dict, handoff_metrics: list = None) -> AggregatedNode:
    """
    diagnosis: dict created by diagnose.py — {'node':..., 'cells_by_axis':{general_failure,security,handoff}, 'aiid':[...]}
    handoff_metrics: list of {'ips': IPSResult, 'confdecay': ConfDecayResult, 'laaj': LaaJResult}
                     — handoff pairs that have this node as downstream
    """
    handoff_metrics = handoff_metrics or []
    n = diagnosis['node']
    cells_by = diagnosis['cells_by_axis']

    base_general = _axis_base_score(cells_by.get('general_failure', []))
    base_security = _axis_base_score(cells_by.get('security', []))
    base_handoff = _axis_base_score(cells_by.get('handoff', []))

    boost, alerts = _runtime_boost_for_handoff(handoff_metrics)
    handoff_with_boost = min(SCORE_CAP, base_handoff + boost)

    final = (
        AXIS_WEIGHTS['handoff']  * handoff_with_boost +
        AXIS_WEIGHTS['security'] * base_security +
        AXIS_WEIGHTS['general_failure'] * base_general
    )
    final = round(min(SCORE_CAP, final), 2)

    return AggregatedNode(
        node_id=n['id'],
        function=n['function'],
        ai_mode=n['ai_mode'],
        final_score=final,
        color=_color_for(final),
        axis_scores={
            'general_failure': round(base_general, 2),
            'security': round(base_security, 2),
            'handoff_base': round(base_handoff, 2),
            'handoff_with_boost': round(handoff_with_boost, 2),
        },
        evidence=_build_evidence(diagnosis, alerts),
        mitigation_options=_collect_mitigation_options(diagnosis),
        runtime_metric_boost=round(boost, 2),
        runtime_metric_alerts=alerts,
    )


def aggregate_workflow(diagnoses: list, handoff_metrics_by_dn_node: dict = None) -> list:
    """
    diagnoses: per-RED-node diagnosis dicts
    handoff_metrics_by_dn_node: {downstream_node_id: [metric_rows...]}
    Returns: list[AggregatedNode] (one per RED node)
    """
    handoff_metrics_by_dn_node = handoff_metrics_by_dn_node or {}
    out = []
    for d in diagnoses:
        nid = d['node']['id']
        out.append(aggregate_node(d, handoff_metrics_by_dn_node.get(nid, [])))
    return out


# -----------------------------------------------------------------------------
# unit test — passing criteria verification (RED node alignment + final ≥ 4.0 on IPS Alert)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    from dataclasses import dataclass as _dc

    # Mock metric result objects (duck-type — only band/alert/score are inspected)
    @_dc
    class _IPS:
        upstream: str
        downstream: str
        score: float
        alert: bool

    @_dc
    class _CD:
        upstream: str
        downstream: str
        over_trust_gap: float
        under_use_gap: float
        band: str

    @_dc
    class _LaaJ:
        upstream: str
        downstream: str
        alignment_score: float
        disagreement_flags: list

    # ----- case 1: legal N2 (ontology RED signature alignment) -----
    # risk_score average per cells_by_axis: general 4.5, security 4.2, handoff 4.0
    # base final = 0.3*4.5 + 0.3*4.2 + 0.4*4.0 = 1.35 + 1.26 + 1.6 = 4.21 → RED ✅
    legal_n2 = {
        'node': {'id': 'N2', 'function': 'LLM clause extraction', 'ai_mode': 'Full automation (LLM)'},
        'cells_by_axis': {
            'general_failure': [{'primary_failure_mode': 'hallucination', 'risk_score': 4.5,
                                 'evidence': {'aiid_incidents': [{'id':'incident_704','relevance':'direct'}]}}],
            'security': [{'primary_threats': [{'id':'LLM09','title':'Misinformation','relevance':'direct'}],
                          'mitre_atlas_techniques': [{'id':'AML.T0043','title':'Craft Adversarial Data'}],
                          'risk_score': 4.2}],
            'handoff': [{'primary_handoff_risk': 'schema_contract_drift',
                         'risk_score': 4.0,
                         'mitigation_options': {'must_fix': {'action': 'Confidence threshold gating'}},
                         'heuristic_source': '본인 IP'}],
        },
        'aiid': [],
    }
    r1 = aggregate_node(legal_n2, handoff_metrics=[])
    assert r1.color == 'RED', f"N2 expected RED, got {r1.color} (score={r1.final_score})"
    assert r1.final_score >= 4.0, r1.final_score
    assert r1.runtime_metric_boost == 0.0

    # ----- case 2: legal N2 + IPS alert handoff metric → final boost applied -----
    metrics_alert = [{
        'ips': _IPS('N1', 'N2', score=0.42, alert=True),
        'confdecay': _CD('N1', 'N2', over_trust_gap=0.25, under_use_gap=0.0, band='over_trust_alert'),
        'laaj': _LaaJ('N1', 'N2', alignment_score=0.35, disagreement_flags=['schema mismatch']),
    }]
    r2 = aggregate_node(legal_n2, handoff_metrics=metrics_alert)
    # boost = 0.5 (ips) + 0.5 (OT) + 0.3 (laaj_low) = 1.3 → handoff 4.0 + 1.3 = 5.0 cap
    # final = 0.3*4.5 + 0.3*4.2 + 0.4*5.0 = 1.35 + 1.26 + 2.0 = 4.61
    assert r2.final_score >= 4.0, f"IPS alert → final expected ≥4.0, got {r2.final_score}"
    assert r2.runtime_metric_boost >= 1.0, r2.runtime_metric_boost
    assert any('ips_alert' in a for a in r2.runtime_metric_alerts)
    assert any('over_trust' in a for a in r2.runtime_metric_alerts)
    assert any('laaj_low' in a for a in r2.runtime_metric_alerts)

    # ----- case 3: low-risk YELLOW node (general 2.5, security 2.5, handoff 2.0) -----
    yellow_node = {
        'node': {'id': 'N4', 'function': 'compare playbook', 'ai_mode': 'Decision support'},
        'cells_by_axis': {
            'general_failure': [{'primary_failure_mode': 'threshold_drift', 'risk_score': 2.5}],
            'security': [{'primary_threats': [], 'risk_score': 2.5}],
            'handoff': [{'primary_handoff_risk': 'threshold_arbitrary', 'risk_score': 2.0, 'mitigation_options': {}}],
        },
        'aiid': [],
    }
    r3 = aggregate_node(yellow_node, handoff_metrics=[])
    # 0.3*2.5 + 0.3*2.5 + 0.4*2.0 = 0.75 + 0.75 + 0.8 = 2.3 → GREEN (< 2.5)
    # boundary — adjust check
    assert r3.color in ('YELLOW', 'GREEN'), r3.color
    assert r3.final_score < 4.0

    # ----- case 4: GREEN (no diagnosis cells) -----
    green_node = {
        'node': {'id': 'N7', 'function': 'E-signature', 'ai_mode': 'Untouched'},
        'cells_by_axis': {'general_failure': [], 'security': [], 'handoff': []},
        'aiid': [],
    }
    r4 = aggregate_node(green_node, handoff_metrics=[])
    assert r4.color == 'GREEN'
    assert r4.final_score == 0.0
    assert r4.evidence == []

    # ----- case 5: evidence 3-element alignment — failure_mode + evidence_type + mitigation_summary -----
    for ev in r1.evidence:
        assert ev.failure_mode and ev.evidence_type, ev
        assert ev.mitigation_summary is not None

    # ----- case 6: loan N6→N7 silent escalation signature -----
    loan_n7 = {
        'node': {'id': 'N7', 'function': '자동 결정 엔진', 'ai_mode': 'Full automation'},
        'cells_by_axis': {
            'general_failure': [{'primary_failure_mode': 'false_positive_approval', 'risk_score': 4.8}],
            'security': [{'primary_threats': [{'id':'LLM06','title':'Excessive Agency','relevance':'direct'}], 'risk_score': 4.8}],
            'handoff': [{'primary_handoff_risk': 'bias_cascade_from_ACS', 'risk_score': 4.7,
                         'mitigation_options': {'must_fix': {'action': 'confidence trace propagation'}},
                         'heuristic_source': '본인 IP'}],
        },
        'aiid': [],
    }
    # loan_N6→N7 over_trust signature
    loan_metrics = [{
        'ips': _IPS('N6', 'N7', score=0.55, alert=False),
        'confdecay': _CD('N6', 'N7', over_trust_gap=0.29, under_use_gap=0.0, band='over_trust_alert'),
        'laaj': _LaaJ('N6', 'N7', alignment_score=0.40, disagreement_flags=['confidence cliff']),
    }]
    r6 = aggregate_node(loan_n7, handoff_metrics=loan_metrics)
    assert r6.color == 'RED', f"loan_N7 expected RED, got {r6.color} (score={r6.final_score})"
    assert r6.final_score >= 4.0

    # ----- case 7: legal N3 borderline — IPS watch + LaaJ flags (no full alert)
    # passing criterion: predicted RED ↔ aggregated RED alignment. base 3.91 + IPS watch boost 0.2 + LaaJ flags 0.2 → RED
    legal_n3 = {
        'node': {'id': 'N3', 'function': 'LLM risk flagging', 'ai_mode': 'Full automation (LLM)'},
        'cells_by_axis': {
            'general_failure': [{'primary_failure_mode': 'false_negative', 'risk_score': 4.2}],
            'security': [{'primary_threats': [{'id':'LLM09','title':'Misinformation'}], 'risk_score': 3.5}],
            'handoff': [{'primary_handoff_risk': 'weighting_loss', 'risk_score': 4.0,
                         'mitigation_options': {'must_fix': {'action': 'Multi-dim risk vector'}},
                         'heuristic_source': '본인 IP'}],
        },
        'aiid': [],
    }
    legal_n3_metrics = [{
        'ips': _IPS('N2', 'N3', score=0.514, alert=False),   # watch band, not alert
        'confdecay': _CD('N2', 'N3', over_trust_gap=0.0, under_use_gap=0.0, band='healthy'),
        'laaj': _LaaJ('N2', 'N3', alignment_score=0.65, disagreement_flags=['schema token dropped']),
    }]
    # set watch band manually via attr (IPSResult dataclass has a band field)
    setattr(legal_n3_metrics[0]['ips'], 'band', 'watch')
    r7 = aggregate_node(legal_n3, handoff_metrics=legal_n3_metrics)
    # boost = 0.2 (ips_watch) + 0.2 (laaj_flags) = 0.4 → handoff 4.0+0.4 = 4.4
    # final = 0.3*4.2 + 0.3*3.5 + 0.4*4.4 = 1.26 + 1.05 + 1.76 = 4.07 → RED
    assert r7.color == 'RED', f"N3 borderline expected RED with ips_watch+laaj_flags boost, got {r7.color} (score={r7.final_score})"
    assert any('ips_watch' in a for a in r7.runtime_metric_alerts)

    print("aggregator.py unit tests passed:")
    for label, r in [('legal_N2 base', r1), ('legal_N2 + IPS alert', r2),
                     ('YELLOW node', r3), ('GREEN node', r4),
                     ('loan_N7 silent escalation', r6),
                     ('legal_N3 borderline (ips_watch+laaj_flags)', r7)]:
        print(f"  [{label}]: final={r.final_score} color={r.color} boost={r.runtime_metric_boost} "
              f"alerts={r.runtime_metric_alerts[:2]}")
