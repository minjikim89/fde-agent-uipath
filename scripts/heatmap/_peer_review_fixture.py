"""
Gemini Critic Disagreement Fixture (BRAIN_PEER=mock, Wave 2 · Rapid path)
========================================================================

Deterministic per-node synthesis for the *Gemini primary vs Gemini critic
(self-critique)* disagreement heatmap. Used by `peer_review_disagreement.py`
until the real Gemini API key arrives (then this fixture can be swapped for
a runtime call to Gemini Pro critic against Gemini primary).

Model policy (architecture.md §2, 2026-05-29):
  - Rapid Agent = Gemini-only. Sub-Agent 6 = Gemini self-critique (Pro↔Pro
    or Flash↔Pro adversarial 2nd-pass).
  - UiPath path = multi-model (Claude ↔ Gemini) — owns a separate fixture.

Code-identifier preservation note
---------------------------------
The dict key `node["claude"]` is retained as a **code identifier** even
though the critic model is now Gemini self-critique. Renaming would break
the viz layer (peer_review_disagreement.py imports + indexes by this key)
and the Phoenix attribute schema freeze (dashboard_config.json line 134).
UI labels are the only swap.

Why a fixture (and not sub_agent_6_peer_review._mock_response_from)?
  - sub_agent_6 mock backend operates on a single RED node and returns a
    heuristic critic review. Heatmap needs per-node *side-by-side comparison*
    across the entire workflow (9 legal nodes / 11 loan nodes) with explicit
    mitigation-set diff and evidence-set diff fields — these are not surfaced
    by the critic backend (axis_scores only).
  - File-scope isolation rule (Wave 2 brief): no edits to sub_agent_6_*.

Data schema per node:
  node_id        : str   (e.g. "N3", "N5a")
  function       : str   (short label, matches v0.4 diagnosis output)
  ai_mode        : str   (Full automation / Decision support / HITL / Manual)
  predicted_color: str   ("RED" | "YELLOW" | "GREEN")
  gemini         : {                                    # Gemini primary
      aggregate_risk:    float in [0, 5],
      mitigations:       list[str],   # primary recommender selections
      evidence_aiid:     list[str],   # primary RAG top-k incident IDs
  }
  claude         : {                                    # Gemini critic (key name preserved)
      axis_scores: {alignment, coverage, hallucination_risk} each in [0, 5],
      peer_confidence: float in [0, 1],   # mean(axis_scores)/5
      alternative_view: str,
      disagreement_flags: list[str],
      mitigations_alt: list[str],          # critic's alternative mitigation set
      evidence_alt:    list[str],          # critic's alternative AIID picks
  }
  derived (computed by render layer):
      claude_score = mean(axis_scores)               # [0, 5] same scale as Gemini
      delta        = gemini_score - claude_score     # signed
      flagged      = abs(delta) > 0.5
      mitigation_diff = symmetric set difference     # |G △ C|
      evidence_diff   = symmetric set difference     # |G △ C|

Calibration intent
------------------
RED nodes from v0.4 diagnosis (legal: N2/N3/N5a; loan: N6/N7/N9) anchor the
disagreement signal so the demo narrative ("our diagnosis is self-diagnosed")
lands on the Phase 1 영상 1:15-1:35 timeline. YELLOW/GREEN nodes show low
delta (consensus) — most rows are quiet so the RED rows pop visually.
"""

from __future__ import annotations
from typing import Any


# ---------------------------------------------------------------------------
# Legal — 9 nodes (matches sample-workflows/legal-contract-review-v0.1.md v0.2)
# ---------------------------------------------------------------------------

LEGAL_NODES: list[dict[str, Any]] = [
    {
        "node_id": "N1",
        "function": "Document Ingestion",
        "ai_mode": "Manual",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.2,
            "mitigations": ["doc_validation", "format_check"],
            "evidence_aiid": ["incident_010"],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur with primary; minor reservations only — see flags.",
            "disagreement_flags": [],
            "mitigations_alt": ["doc_validation", "format_check"],
            "evidence_alt": ["incident_010"],
        },
    },
    {
        "node_id": "N2",
        "function": "Clause Extraction",
        "ai_mode": "Full automation (LLM)",
        "predicted_color": "RED",
        "gemini": {
            "aggregate_risk": 4.21,
            "mitigations": [
                "human_in_the_loop_review",
                "schema_validation",
                "confidence_threshold_gate",
                "structured_output_constraint",
                "retrieval_grounding",
                "audit_log",
                "fallback_to_template",
            ],
            "evidence_aiid": ["incident_464", "incident_732", "incident_1447"],
        },
        "claude": {
            "axis_scores": {"alignment": 4, "coverage": 4, "hallucination_risk": 4},
            "alternative_view": (
                "N2: Concur with RED verdict but coverage slightly thin — security cell cites "
                "LLM06 alone; for LLM-driven extraction, LLM09 Misinformation should co-anchor."
            ),
            "disagreement_flags": [
                "Security axis cites only LLM06 for N2 — expected LLM09 co-citation given clause extraction pattern.",
            ],
            "mitigations_alt": [
                "human_in_the_loop_review",
                "schema_validation",
                "confidence_threshold_gate",
                "structured_output_constraint",
                "retrieval_grounding",
                "audit_log",
                "fallback_to_template",
                "redundant_extractor_ensemble",   # ← Gemini critic adds
            ],
            "evidence_alt": [
                "incident_464",
                "incident_732",
                "incident_1447",
                "incident_858",   # ← Gemini critic adds
            ],
        },
    },
    {
        "node_id": "N3",
        "function": "Risk Flagging",
        "ai_mode": "Full automation (LLM)",
        "predicted_color": "RED",
        "gemini": {
            "aggregate_risk": 4.07,
            "mitigations": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "second_model_cross_check",
                "audit_log",
                "explainability_layer",
                "domain_expert_calibration",
                "fallback_to_manual",
            ],
            "evidence_aiid": ["incident_364", "incident_858", "incident_738"],
        },
        "claude": {
            # Anchor 2 from peer_review_prompt.md (legal N3 borderline disagree)
            "axis_scores": {"alignment": 3, "coverage": 3, "hallucination_risk": 2},
            "alternative_view": (
                "N3: legal vertical — Gemini critic expects EU AI Act high-risk citation if contract decision "
                "is auto-executed. Verdict sits on RED/YELLOW boundary; runtime ips_watch + laaj_flags drove "
                "the +0.4 boost, design-time base is 3.7 YELLOW."
            ),
            "disagreement_flags": [
                "Security axis cites only LLM09 for N3 — expected LLM06 Excessive Agency co-citation given binary flag classifier.",
                "AIID top-5 mean similarity 0.50 — RAG evidence suggestive, not corroborative.",
                "Verdict RED borderline (agg=4.07); design-time base is YELLOW absent runtime metric boost.",
            ],
            "mitigations_alt": [
                "human_in_the_loop_review",
                "second_model_cross_check",
                "audit_log",
                "explainability_layer",
                "domain_expert_calibration",
                "fallback_to_manual",
                "uncertainty_aware_routing",     # ← Gemini critic-only
                "regulatory_anchor_citation",    # ← Gemini critic-only (EU AI Act 5(b))
            ],
            "evidence_alt": [
                "incident_364",
                "incident_738",
                "incident_188",   # ← Gemini critic-only (ranking bias parallel)
                "incident_222",   # ← Gemini critic-only (moderation FP)
            ],
        },
    },
    {
        "node_id": "N4",
        "function": "Standard Clauses Comparison",
        "ai_mode": "Decision support",
        "predicted_color": "YELLOW",
        "gemini": {
            "aggregate_risk": 3.1,
            "mitigations": ["diff_audit", "version_control", "reviewer_signoff"],
            "evidence_aiid": ["incident_122"],
        },
        "claude": {
            "axis_scores": {"alignment": 4, "coverage": 3, "hallucination_risk": 4},
            "alternative_view": "Concur YELLOW; decision-support pattern with reviewer signoff well-anchored.",
            "disagreement_flags": [],
            "mitigations_alt": ["diff_audit", "version_control", "reviewer_signoff"],
            "evidence_alt": ["incident_122"],
        },
    },
    {
        "node_id": "N5a",
        "function": "Auto-Approve Path",
        "ai_mode": "Full automation",
        "predicted_color": "RED",
        "gemini": {
            "aggregate_risk": 4.88,
            "mitigations": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "circuit_breaker",
                "audit_log",
                "rollback_capability",
                "regulatory_pre_check",
            ],
            "evidence_aiid": ["incident_316", "incident_748", "incident_245"],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 5, "hallucination_risk": 4},
            "alternative_view": "Concur with primary RED; auto-approve handoff well-supported across all 3 axes.",
            "disagreement_flags": [],
            "mitigations_alt": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "circuit_breaker",
                "audit_log",
                "rollback_capability",
                "regulatory_pre_check",
            ],
            "evidence_alt": ["incident_316", "incident_748", "incident_245"],
        },
    },
    {
        "node_id": "N5b",
        "function": "Manual Review Path",
        "ai_mode": "HITL",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.6,
            "mitigations": ["reviewer_training", "queue_sla"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — HITL path is the canonical safe lane.",
            "disagreement_flags": [],
            "mitigations_alt": ["reviewer_training", "queue_sla"],
            "evidence_alt": [],
        },
    },
    {
        "node_id": "N6",
        "function": "Negotiation Draft",
        "ai_mode": "Decision support",
        "predicted_color": "YELLOW",
        "gemini": {
            "aggregate_risk": 2.8,
            "mitigations": ["draft_disclaimer", "track_changes", "reviewer_signoff"],
            "evidence_aiid": ["incident_205"],
        },
        "claude": {
            "axis_scores": {"alignment": 3, "coverage": 3, "hallucination_risk": 3},
            "alternative_view": (
                "N6: Concur YELLOW broadly. Coverage borderline — handoff cell to N7 (counterparty exchange) "
                "should be explicit; currently implicit."
            ),
            "disagreement_flags": [
                "Handoff axis thin for N6 — N6→N7 counterparty exchange edge has no explicit cell.",
            ],
            "mitigations_alt": [
                "draft_disclaimer",
                "track_changes",
                "reviewer_signoff",
                "explicit_handoff_schema",   # ← Gemini critic adds
            ],
            "evidence_alt": ["incident_205"],
        },
    },
    {
        "node_id": "N7",
        "function": "Counterparty Exchange",
        "ai_mode": "Manual",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.4,
            "mitigations": ["secure_channel", "version_lock"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 4, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — manual exchange with secure channel.",
            "disagreement_flags": [],
            "mitigations_alt": ["secure_channel", "version_lock"],
            "evidence_alt": [],
        },
    },
    {
        "node_id": "N8",
        "function": "Final Archival",
        "ai_mode": "Manual",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.0,
            "mitigations": ["retention_policy", "access_control"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — archival is policy-bound, low AI surface.",
            "disagreement_flags": [],
            "mitigations_alt": ["retention_policy", "access_control"],
            "evidence_alt": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Korean Loan — 11 nodes (matches sample-workflows/loan-underwriting-kr-v0.1.md v0.2)
# ---------------------------------------------------------------------------

LOAN_NODES: list[dict[str, Any]] = [
    {
        "node_id": "N1",
        "function": "Loan Application Intake",
        "ai_mode": "Manual",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.1,
            "mitigations": ["pii_redaction", "consent_capture"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — intake is consent-anchored.",
            "disagreement_flags": [],
            "mitigations_alt": ["pii_redaction", "consent_capture"],
            "evidence_alt": [],
        },
    },
    {
        "node_id": "N2",
        "function": "OCR Document Parsing",
        "ai_mode": "Decision support",
        "predicted_color": "YELLOW",
        "gemini": {
            "aggregate_risk": 3.2,
            "mitigations": ["ocr_confidence_gate", "human_verification_sample"],
            "evidence_aiid": ["incident_077"],
        },
        "claude": {
            "axis_scores": {"alignment": 4, "coverage": 3, "hallucination_risk": 4},
            "alternative_view": "Concur YELLOW; OCR pattern standard.",
            "disagreement_flags": [],
            "mitigations_alt": ["ocr_confidence_gate", "human_verification_sample"],
            "evidence_alt": ["incident_077"],
        },
    },
    {
        "node_id": "N3",
        "function": "Credit Bureau Pull",
        "ai_mode": "Decision support",
        "predicted_color": "YELLOW",
        "gemini": {
            "aggregate_risk": 3.0,
            "mitigations": ["kcb_nice_dual_source", "stale_data_check"],
            "evidence_aiid": ["incident_704"],
        },
        "claude": {
            "axis_scores": {"alignment": 4, "coverage": 4, "hallucination_risk": 3},
            "alternative_view": "Concur YELLOW; dual-source mitigation appropriate.",
            "disagreement_flags": [],
            "mitigations_alt": ["kcb_nice_dual_source", "stale_data_check"],
            "evidence_alt": ["incident_704"],
        },
    },
    {
        "node_id": "N4",
        "function": "Alternative Credit Scoring (ACS)",
        "ai_mode": "Decision support",
        "predicted_color": "YELLOW",
        "gemini": {
            "aggregate_risk": 3.4,
            "mitigations": [
                "feature_attribution_shap",
                "bias_audit_quarterly",
                "fairness_constraint",
            ],
            "evidence_aiid": ["incident_602"],
        },
        "claude": {
            "axis_scores": {"alignment": 3, "coverage": 3, "hallucination_risk": 4},
            "alternative_view": (
                "N4: Concur YELLOW but coverage borderline — handoff to N7 (auto-decision) should explicitly "
                "carry SHAP attribution. Currently SHAP factor may be dropped at boundary."
            ),
            "disagreement_flags": [
                "Handoff axis for N4 cites SHAP attribution but does not anchor preservation across N4→N7 edge.",
            ],
            "mitigations_alt": [
                "feature_attribution_shap",
                "bias_audit_quarterly",
                "fairness_constraint",
                "shap_preservation_schema",   # ← Gemini critic adds
            ],
            "evidence_alt": ["incident_602"],
        },
    },
    {
        "node_id": "N5",
        "function": "Income Verification",
        "ai_mode": "Decision support",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.8,
            "mitigations": ["national_tax_api_verify"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — national tax API is canonical anchor.",
            "disagreement_flags": [],
            "mitigations_alt": ["national_tax_api_verify"],
            "evidence_alt": [],
        },
    },
    {
        "node_id": "N6",
        "function": "LLM Risk Analysis",
        "ai_mode": "Full automation (LLM)",
        "predicted_color": "RED",
        "gemini": {
            "aggregate_risk": 4.24,
            "mitigations": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "retrieval_grounding",
                "second_model_cross_check",
                "audit_log",
                "explainability_layer",
                "fallback_to_manual",
                "regulatory_anchor_citation",
                "uncertainty_aware_routing",
            ],
            "evidence_aiid": ["incident_464", "incident_309", "incident_736"],
        },
        "claude": {
            "axis_scores": {"alignment": 3, "coverage": 3, "hallucination_risk": 5},
            "alternative_view": (
                "N6: korean_loan — Gemini critic expects explicit K-PIPA Art 22-2 anchor on handoff axis; primary's "
                "evidence (incident_309 facial recognition + incident_736 LLM phishing) is *thematic* not "
                "*domain-specific* to credit risk. RAG drifted off-vertical."
            ),
            "disagreement_flags": [
                "AIID evidence drift: incident_309 (facial recognition) + incident_736 (phishing) are off-domain for credit risk analysis.",
                "K-PIPA Art 22-2 / 공정대출법 anchors not surfaced in any axis cell — korean_loan workflow requires explicit citation.",
            ],
            "mitigations_alt": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "retrieval_grounding",
                "second_model_cross_check",
                "audit_log",
                "explainability_layer",
                "fallback_to_manual",
                "domain_specific_rag_filter",      # ← Gemini critic adds (Q7-style)
                "kpipa_22_2_compliance_check",    # ← Gemini critic adds
            ],
            "evidence_alt": [
                "incident_464",
                "incident_911",   # Apple Card gender bias — true domain match
                "incident_704",   # Algorithmic redlining
                "incident_602",   # Bias cascade consumer credit
            ],
        },
    },
    {
        "node_id": "N7",
        "function": "Auto-Decision Engine",
        "ai_mode": "Full automation",
        "predicted_color": "RED",
        "gemini": {
            "aggregate_risk": 4.88,
            "mitigations": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "circuit_breaker",
                "audit_log",
                "rollback_capability",
                "regulatory_pre_check",
                "kpipa_art_22_2_optout",
                "bias_cascade_monitor",
                "shap_preservation_schema",
            ],
            "evidence_aiid": ["incident_748", "incident_310", "incident_1467"],
        },
        "claude": {
            # Anchor 1 from peer_review_prompt.md (loan_N7 silent escalation, concur)
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 4},
            "alternative_view": "Concur with primary RED; all 3 axes well-supported, K-PIPA anchor present on handoff.",
            "disagreement_flags": [],
            "mitigations_alt": [
                "human_in_the_loop_review",
                "confidence_threshold_gate",
                "circuit_breaker",
                "audit_log",
                "rollback_capability",
                "regulatory_pre_check",
                "kpipa_art_22_2_optout",
                "bias_cascade_monitor",
                "shap_preservation_schema",
            ],
            "evidence_alt": ["incident_748", "incident_310", "incident_1467"],
        },
    },
    {
        "node_id": "N8",
        "function": "Internal Approval Queue",
        "ai_mode": "HITL",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.5,
            "mitigations": ["sla_monitoring", "reviewer_load_balance"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — HITL queue is the safe lane.",
            "disagreement_flags": [],
            "mitigations_alt": ["sla_monitoring", "reviewer_load_balance"],
            "evidence_alt": [],
        },
    },
    {
        "node_id": "N9",
        "function": "Rejection Letter Generation",
        "ai_mode": "Full automation (LLM)",
        "predicted_color": "RED",
        "gemini": {
            "aggregate_risk": 4.41,
            "mitigations": [
                "template_constrained_generation",
                "human_signoff_for_rejection",
                "reasoning_traceability",
                "audit_log",
                "regulatory_phrasing_check",
                "kpipa_explanation_right",
                "fallback_to_template",
            ],
            "evidence_aiid": ["incident_464", "incident_532", "incident_732"],
        },
        "claude": {
            # Anchor 3 pattern (handoff axis thin)
            "axis_scores": {"alignment": 3, "coverage": 2, "hallucination_risk": 4},
            "alternative_view": (
                "N9: Concur RED on alignment, but coverage gap — handoff axis missing despite N7→N9 carrying "
                "the auto-decision rationale that must be reproduced verbatim. Boundary risk implicit."
            ),
            "disagreement_flags": [
                "Handoff axis missing for N9 — N7→N9 decision-rationale handoff has no explicit cell.",
                "K-PIPA Art 22-2 (자동결정 거부권 explanation right) cited in mitigations but not anchored in axis cells.",
            ],
            "mitigations_alt": [
                "template_constrained_generation",
                "human_signoff_for_rejection",
                "reasoning_traceability",
                "audit_log",
                "regulatory_phrasing_check",
                "kpipa_explanation_right",
                "fallback_to_template",
                "decision_rationale_passthrough",   # ← Gemini critic adds
                "n7_to_n9_handoff_schema",          # ← Gemini critic adds
            ],
            "evidence_alt": [
                "incident_464",
                "incident_532",
                "incident_1467",   # ← Gemini critic adds (fictitious refs — directly applicable)
                "incident_911",    # ← Gemini critic adds (loan bias — explanation right context)
            ],
        },
    },
    {
        "node_id": "N10",
        "function": "Customer Notification (SMS/App)",
        "ai_mode": "Decision support",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.7,
            "mitigations": ["pii_redaction_at_send", "delivery_audit"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 4, "coverage": 4, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN; notification path is templated.",
            "disagreement_flags": [],
            "mitigations_alt": ["pii_redaction_at_send", "delivery_audit"],
            "evidence_alt": [],
        },
    },
    {
        "node_id": "N11",
        "function": "Audit Log Archival",
        "ai_mode": "Manual",
        "predicted_color": "GREEN",
        "gemini": {
            "aggregate_risk": 1.0,
            "mitigations": ["immutable_log_store", "kofiu_retention_policy"],
            "evidence_aiid": [],
        },
        "claude": {
            "axis_scores": {"alignment": 5, "coverage": 5, "hallucination_risk": 5},
            "alternative_view": "Concur GREEN — audit retention is policy-bound.",
            "disagreement_flags": [],
            "mitigations_alt": ["immutable_log_store", "kofiu_retention_policy"],
            "evidence_alt": [],
        },
    },
]


FIXTURE: dict[str, list[dict[str, Any]]] = {
    "legal": LEGAL_NODES,
    "loan": LOAN_NODES,
}


# ---------------------------------------------------------------------------
# Computed fields
# ---------------------------------------------------------------------------

DELTA_FLAG_THRESHOLD = 0.5


def claude_score(node: dict) -> float:
    """Mean of Gemini critic axis_scores (0~5), same scale as Gemini primary
    aggregate_risk. NOTE: function name preserved as code identifier."""
    a = node["claude"]["axis_scores"]
    return round((a["alignment"] + a["coverage"] + a["hallucination_risk"]) / 3.0, 2)


def peer_confidence(node: dict) -> float:
    """mean(axis_scores)/5 in [0, 1] — same formula as Sub-Agent 6."""
    return round(claude_score(node) / 5.0, 2)


def delta(node: dict) -> float:
    """gemini_score - claude_score (signed)."""
    return round(node["gemini"]["aggregate_risk"] - claude_score(node), 2)


def is_flagged(node: dict) -> bool:
    return abs(delta(node)) > DELTA_FLAG_THRESHOLD


def mitigation_diff(node: dict) -> dict:
    """Symmetric set difference for mitigations + per-side missing counts."""
    g = set(node["gemini"]["mitigations"])
    c = set(node["claude"]["mitigations_alt"])
    return {
        "gemini_only": sorted(g - c),
        "claude_only": sorted(c - g),
        "diff_count": len(g ^ c),
        "union_count": len(g | c),
    }


def evidence_diff(node: dict) -> dict:
    """Symmetric set difference for AIID evidence picks."""
    g = set(node["gemini"]["evidence_aiid"])
    c = set(node["claude"]["evidence_alt"])
    return {
        "gemini_only": sorted(g - c),
        "claude_only": sorted(c - g),
        "diff_count": len(g ^ c),
        "union_count": len(g | c),
    }


def workflow_summary(sample: str) -> dict:
    nodes = FIXTURE[sample]
    flagged = sum(1 for n in nodes if is_flagged(n))
    return {
        "sample": sample,
        "node_count": len(nodes),
        "flagged_count": flagged,
        "consensus_count": len(nodes) - flagged,
        "disagreement_count": flagged,   # crew-level Phoenix attribute
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    for sample in ("legal", "loan"):
        s = workflow_summary(sample)
        print(f"[{sample}] {s['node_count']} nodes · "
              f"flagged={s['flagged_count']} · consensus={s['consensus_count']}")
        for n in FIXTURE[sample]:
            cs = claude_score(n)
            d = delta(n)
            md = mitigation_diff(n)
            ed = evidence_diff(n)
            mark = "🚩" if is_flagged(n) else "  "
            print(f"  {mark} {n['node_id']:4s} {n['predicted_color']:6s} "
                  f"G={n['gemini']['aggregate_risk']:.2f} C={cs:.2f} "
                  f"Δ={d:+.2f} mit_diff={md['diff_count']} ev_diff={ed['diff_count']}")

    # Invariants
    legal_summary = workflow_summary("legal")
    loan_summary = workflow_summary("loan")
    assert legal_summary["node_count"] == 9, legal_summary
    assert loan_summary["node_count"] == 11, loan_summary
    assert legal_summary["flagged_count"] >= 1, "expected ≥1 flagged in legal"
    assert loan_summary["flagged_count"] >= 1, "expected ≥1 flagged in loan"
    # N3 legal must be flagged (anchor from peer_review_prompt.md)
    n3 = next(n for n in LEGAL_NODES if n["node_id"] == "N3")
    assert is_flagged(n3), f"N3 must be flagged (anchor), delta={delta(n3)}"
    # N7 loan must be consensus (anchor 1)
    n7 = next(n for n in LOAN_NODES if n["node_id"] == "N7")
    assert not is_flagged(n7), f"N7 must be consensus (anchor 1), delta={delta(n7)}"
    print("\n_peer_review_fixture.py smoke test passed.")
