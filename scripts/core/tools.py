"""
FDE Agent — Shared Diagnosis Tool Functions (import-safe, dependency-injected)
==============================================================================

★ These are THE functions both orchestration layers wrap:
    - ADK (Rapid)     : each becomes an ADK FunctionTool (external brain)
    - CrewAI (UiPath) : each becomes a CrewAI @tool      (multi-model brain)

Design rule (vs the old diagnose.py): NO module-level mutable state. Every tool
takes its dependencies explicitly. `ontology_lookup` takes the `cells` list,
`retrieve_incidents` takes an `embed_fn` + `collection`. This is what makes them
`import`-callable without running a pipeline `main()` first.

The heavy resources (Chroma collection, BGE-M3 model) are owned by
`engine.DiagnosisEngine`, which injects them into these tools. The tools
themselves never import chromadb / sentence-transformers, so they stay cheap to
import in a lint/docs/UiPath-pack environment.

Regression contract: outputs are byte-identical to diagnose.py v0.2 for the
bundled legal/loan samples (same parse, same ontology grouping, same metric
synthesis, same aggregator weights).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

# scripts/ is placed on sys.path by core/__init__.py — absolute imports match the
# convention used across the diagnosis core and serve layers.
from metrics.ips import compute_ips
from metrics.confdecay import compute_confdecay
# metrics.laaj is imported lazily inside compute_handoff_metric so this module stays
# importable in builds that exclude the LaaJ backend (e.g. a lean OSS export).
from agents.aggregator import aggregate_workflow, aggregate_node  # noqa: F401  (re-export)


EmbedFn = Callable[[str], list]


# =============================================================
# 1) parse_workflow — pure (no deps)
# =============================================================

def parse_workflow(content: str) -> list:
    """Parse a Layer-2 workflow markdown string → list of node dicts.

    Reads the '## Node Inventory' table. Pure: takes raw text, not a path, so
    callers (CLI / FastAPI / UiPath payload / CrewAI tool) all converge here.

    Returns: list[{id, function, ai_mode, predicted_color, prediction_text}]
    """
    nodes = []
    in_section = False
    for line in content.split("\n"):
        if "## Node Inventory" in line:
            in_section = True
            continue
        if in_section and line.startswith("## ") and "Node Inventory" not in line:
            break
        if in_section and line.startswith("|") and "|" in line[1:]:
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) >= 4:
                node_label = parts[0]
                m = re.search(r"(N\w+)", node_label.replace("**", ""))
                if m and m.group(1) not in ("Node",):
                    prediction = parts[3]
                    color = "RED" if "RED" in prediction else ("YELLOW" if "YELLOW" in prediction else "GREEN")
                    # English node name = label cell minus the leading id token
                    # ("N2 Clause Extraction" → "Clause Extraction"). Front submits EN.
                    cleaned = node_label.replace("**", "").strip()
                    en_label = re.sub(r"^" + re.escape(m.group(1)) + r"\b[\s:.\-]*", "", cleaned).strip()
                    nodes.append({
                        "id": m.group(1),
                        "label": en_label or m.group(1),
                        "function": parts[1],
                        "ai_mode": parts[2],
                        "predicted_color": color,
                        "prediction_text": prediction[:200],
                    })
    return nodes


def count_ai_intervention(nodes: list) -> int:
    """Heatmap helper — count nodes where AI touches the step (not 'Untouched')."""
    return sum(1 for n in nodes if "untouched" not in (n.get("ai_mode") or "").lower())


def _norm_node_token(tok: str) -> str:
    """Mermaid node reference → bare node id.

    'Start([Vendor...])' → 'Start', 'N6send[/"..."/]' → 'N6send', 'D1' → 'D1'.
    Cuts at the first label-bracket / paren / brace / quote / whitespace so the
    leading id survives.
    """
    tok = tok.strip()
    for sep in ("([", "[", "(", "{", '"', " ", "\t"):
        idx = tok.find(sep)
        if idx > 0:
            tok = tok[:idx]
    return tok.strip()


def extract_edges(content: str) -> list:
    """Directed edges from a Mermaid flowchart block in the workflow markdown.

    Returns list[[src_id, dst_id]] with endpoints normalized to bare node ids.
    Handles labelled edges ('A -- "label" --> B'). Pure (no deps). This is the
    full diagram topology (may include Start/End/decision gateway ids that are not
    in the Node Inventory) — presentation layers can bridge those out if needed.
    """
    edges = []
    for line in content.split("\n"):
        if "-->" not in line:
            continue
        left, right = line.split("-->", 1)   # samples carry one arrow per line
        left = left.split("--")[0]            # drop edge label ('-- label -->')
        src = _norm_node_token(left)
        dst = _norm_node_token(right)
        if src and dst and src != dst:
            edges.append([src, dst])
    return edges


# =============================================================
# 2) ontology_lookup — pure (cells injected)
# =============================================================

def cells_for_node(node_id_simple: str, cells: list, sample_source_filter: Optional[str] = None) -> list:
    """Ontology cells whose `node` field matches this node id, optionally
    filtered to a sample_source. `cells` is injected (no module global)."""
    out = []
    for c in cells:
        cn = c.get("node", "")
        if cn.startswith(f"{node_id_simple.lower()}_") or cn.startswith(f"{node_id_simple}_"):
            if sample_source_filter:
                cs = c.get("sample_source", "legal")  # v0.1 cells without tag default to legal
                if cs != sample_source_filter:
                    continue
            out.append(c)
    return out


def ontology_lookup(node_id: str, cells: list, sample_source: Optional[str] = None) -> dict:
    """node → {general_failure: [...], security: [...], handoff: [...]} grouped by axis.

    This is the tool a Risk Diagnoser / Standards Mapper agent calls. Pure."""
    matched = cells_for_node(node_id, cells, sample_source_filter=sample_source)
    by_axis: dict[str, list] = {"general_failure": [], "security": [], "handoff": []}
    for c in matched:
        axis = c.get("axis")
        if axis in by_axis:
            by_axis[axis].append(c)
    return by_axis


# =============================================================
# 3) retrieve_incidents — RAG (embed_fn + collection injected)
# =============================================================

def retrieve_incidents(query: str, embed_fn: EmbedFn, collection: Any, n: int = 5) -> list:
    """AIID similar-incident retrieval. `embed_fn` and `collection` are injected
    by the engine — this tool never imports chromadb / sentence-transformers."""
    emb = embed_fn(query)
    results = collection.query(query_embeddings=[emb], n_results=n)
    out = []
    for i in range(len(results["ids"][0])):
        md = results["metadatas"][0][i]
        out.append({
            "id": results["ids"][0][i],
            "title": md.get("title", ""),
            "similarity": 1 - results["distances"][0][i],
            "date": md.get("date", ""),
            # additive metadata passthrough — lets the BFF synthesize an authentic
            # incident summary (no separate summary field exists in the corpus).
            "deployers": md.get("deployers", ""),
            "harmed": md.get("harmed", ""),
        })
    return out


# =============================================================
# 4) handoff metrics — IPS / ConfDecay / LaaJ (embed_fn injected)
#    Ported verbatim from diagnose.py v0.2 to preserve regression.
# =============================================================

def _synth_node_text(node: dict, sample_source: Optional[str]) -> str:
    parts = [node["id"], node["function"], node["ai_mode"], sample_source or ""]
    return " | ".join(p for p in parts if p)


def _synth_diagnosis_text(diagnosis: dict) -> str:
    n = diagnosis["node"]
    bits = [n["id"], n["function"], n["ai_mode"]]
    for axis_name, axis_cells in diagnosis["cells_by_axis"].items():
        for cell in axis_cells:
            pf = cell.get("primary_failure_mode") or cell.get("primary_handoff_risk") or ""
            if pf:
                bits.append(f"{axis_name}:{pf}")
            for s in cell.get("secondary_failure_modes", []) or []:
                bits.append(s)
    for inc in diagnosis.get("aiid", [])[:3]:
        bits.append(inc["title"][:60])
    return " | ".join(b for b in bits if b)


def _synth_confidence(diagnosis: Optional[dict], role: str) -> float:
    """upstream LLM = lower self-reported conf; downstream auto-decision =
    artificially high (silent escalation signature). Identical to diagnose.py."""
    if diagnosis is None:
        return 0.70
    risk_scores = []
    for axis_cells in diagnosis["cells_by_axis"].values():
        for c in axis_cells:
            rs = c.get("risk_score")
            if isinstance(rs, (int, float)):
                risk_scores.append(rs)
    avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else 3.5
    if role == "upstream":
        # slope 0.15 (was 0.1): high-risk upstream LLM must dip below the over-trust
        # guard (confdecay.OVER_TRUST_UPSTREAM_MAX=0.8) for over-trust to fire —
        # avg_risk 5.0→0.70, 4.5→0.775. At 0.1 the floor was exactly 0.80 (guard is
        # strict `<0.8`), so over_trust_gap was structurally ≡0 on every handoff.
        return round(max(0.55, min(0.85, 1.0 - (avg_risk - 3.0) * 0.15)), 2)
    return round(max(0.85, min(0.99, 0.85 + (avg_risk - 3.0) * 0.05)), 2)


def build_handoff_pairs(diagnoses: list) -> list:
    """Extract upstream→current handoff pairs from diagnosis handoff-axis cells."""
    pairs = []
    diag_by_node = {d["node"]["id"]: d for d in diagnoses}
    for d in diagnoses:
        for cell in d["cells_by_axis"]["handoff"]:
            up_dep = cell.get("upstream_dependency")
            if not up_dep:
                continue
            ups = up_dep if isinstance(up_dep, list) else [up_dep]
            for up in ups:
                up_short = re.match(r"(?:loan_)?(N\w+?)(?:_|$)", up)
                up_id = up_short.group(1) if up_short else up
                pairs.append({
                    "upstream_id": up_id,
                    "upstream_full": up,
                    "downstream_id": d["node"]["id"],
                    "downstream_full": d["node"]["function"][:50],
                    "cell": cell,
                    "downstream_diagnosis": d,
                    "upstream_diagnosis": diag_by_node.get(up_id),
                })
    return pairs


def compute_metrics_for_pair(pair: dict, sample_source: Optional[str], embed_fn: EmbedFn,
                             laaj_backend: str = "mock") -> dict:
    """Compute IPS / ConfDecay / LaaJ for one handoff pair. embed_fn injected."""
    up_diag = pair["upstream_diagnosis"]
    dn_diag = pair["downstream_diagnosis"]
    up_node_proxy = up_diag["node"] if up_diag else {
        "id": pair["upstream_id"],
        "function": pair["upstream_full"],
        "ai_mode": "upstream (no RED diagnosis available)",
    }

    upstream_text = _synth_node_text(up_node_proxy, sample_source)
    downstream_text = _synth_diagnosis_text(dn_diag)

    ips_res = compute_ips(
        upstream_text, downstream_text,
        pair["upstream_id"], pair["downstream_id"],
        embed_fn,
    )

    up_conf = _synth_confidence(up_diag, "upstream")
    dn_conf = _synth_confidence(dn_diag, "downstream")
    cd_res = compute_confdecay(up_conf, dn_conf, pair["upstream_id"], pair["downstream_id"])

    laaj_ctx = {
        "workflow": sample_source,
        "handoff_pair": f"{pair['upstream_id']} → {pair['downstream_id']}",
        "ontology_handoff_risk": pair["cell"].get("primary_handoff_risk", ""),
        "expected_schema": pair["cell"].get("description", "")[:120],
    }
    from metrics.laaj import compute_laaj  # lazy: see module-header note
    laaj_res = compute_laaj(
        laaj_ctx,
        node_a={"id": pair["upstream_id"], "type": "upstream", "output": upstream_text},
        node_b={"id": pair["downstream_id"], "type": "downstream", "output": downstream_text},
        backend=laaj_backend,
    )
    return {"ips": ips_res, "confdecay": cd_res, "laaj": laaj_res, "pair": pair}


def compute_handoff_metrics(diagnoses: list, sample_source: Optional[str], embed_fn: EmbedFn,
                            laaj_backend: str = "mock") -> list:
    """All handoff metric rows for a set of diagnoses (one per pair).

    Each pair is independent and its LaaJ row is a blocking judge call, so pairs run
    CONCURRENTLY — wall-clock is the slowest pair, not the sum. Critical for the live
    demo: the diagnosis dropped from minutes to seconds. ex.map preserves order, so the
    rows stay byte-identical to the previous sequential comprehension.
    """
    pairs = build_handoff_pairs(diagnoses)
    if not pairs:
        return []
    if len(pairs) == 1:
        return [compute_metrics_for_pair(pairs[0], sample_source, embed_fn, laaj_backend)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
        return list(ex.map(
            lambda p: compute_metrics_for_pair(p, sample_source, embed_fn, laaj_backend),
            pairs,
        ))


# =============================================================
# 5) red_nodes — import-safe helper (kept stable for sub_agent_6)
# =============================================================

def red_nodes_from_diagnosis(diag_dict: dict, threshold: float = 4.0) -> list:
    """Node IDs whose aggregated final_score >= threshold (default 4.0 = RED).
    Insertion order preserved. Non-numeric scores treated as 0."""
    out = []
    for nid, info in diag_dict.items():
        if not isinstance(info, dict):
            continue
        try:
            score = float(info.get("final_score", 0) or 0)
        except (TypeError, ValueError):
            continue
        if score >= threshold:
            out.append(nid)
    return out


# =============================================================
# 6) evaluate_hitl — structured (no markdown regex)
#    Replaces coded_agent_wrapper._evaluate_hitl_required, which re-parsed the
#    rendered report. Now operates directly on the structured result.
# =============================================================

def evaluate_hitl(aggregated: list, metric_rows: list, thresholds) -> tuple:
    """Mirror of the Maestro Exclusive Gateway condition, over STRUCTURED data.

    aggregated:  list[AggregatedNode]
    metric_rows: list[{ips, confdecay, laaj, pair}] (from compute_handoff_metrics)
    thresholds:  HitlThresholds

    Returns (hitl_required: bool, reason: str).
    """
    reasons = []

    max_final = max((a.final_score for a in aggregated), default=0.0)
    if max_final >= thresholds.final_score_red:
        reasons.append(f"aggregator.max_final_score={max_final:.2f} >= {thresholds.final_score_red}")

    for row in metric_rows:
        cd = row.get("confdecay")
        if cd is not None and getattr(cd, "over_trust_gap", 0.0) > thresholds.confdecay_over_trust:
            reasons.append(
                f"confdecay over_trust on {cd.upstream}→{cd.downstream} "
                f"(gap={cd.over_trust_gap:.2f} > {thresholds.confdecay_over_trust})"
            )
        ips = row.get("ips")
        if ips is not None and getattr(ips, "score", 1.0) < thresholds.ips_min:
            reasons.append(f"ips.score on {ips.upstream}→{ips.downstream} = {ips.score:.2f} < {thresholds.ips_min}")
        laaj = row.get("laaj")
        if laaj is not None and getattr(laaj, "alignment_score", 1.0) < thresholds.laaj_min:
            reasons.append(
                f"laaj.score on {laaj.upstream}→{laaj.downstream} = "
                f"{laaj.alignment_score:.2f} < {thresholds.laaj_min}"
            )

    total_alerts = sum(
        1 for row in metric_rows
        for k in ("ips", "confdecay", "laaj")
        if row.get(k) is not None and getattr(row[k], "alert", False)
    )
    if total_alerts > 0 and not reasons:
        reasons.append(f"runtime_alerts={total_alerts}")

    return (len(reasons) > 0), " ; ".join(reasons) if reasons else "all-green (no HITL needed)"


def metrics_to_dict(metric_rows: list) -> dict:
    """Collapse metric result objects → JSON-safe {ips/confdecay/laaj: {rows, alerts}}.

    Replaces the markdown-regex extraction in coded_agent_wrapper."""
    def _rows(key, extra):
        rows = []
        alerts = 0
        for r in metric_rows:
            obj = r.get(key)
            if obj is None:
                continue
            is_alert = bool(getattr(obj, "alert", False))
            alerts += int(is_alert)
            rows.append({
                "pair": f"{obj.upstream} → {obj.downstream}",
                "alert": is_alert,
                **extra(obj),
            })
        return {"rows": rows, "alerts": alerts}

    return {
        "ips": _rows("ips", lambda o: {"score": round(getattr(o, "score", 0.0), 3),
                                       "band": getattr(o, "band", "")}),
        "confdecay": _rows("confdecay", lambda o: {
            "over_trust_gap": round(getattr(o, "over_trust_gap", 0.0), 3),
            "under_use_gap": round(getattr(o, "under_use_gap", 0.0), 3),
            "band": getattr(o, "band", "")}),
        "laaj": _rows("laaj", lambda o: {
            "alignment_score": round(getattr(o, "alignment_score", 0.0), 3),
            "disagreement_flags": list(getattr(o, "disagreement_flags", []))[:3]}),
    }


__all__ = [
    "parse_workflow", "count_ai_intervention",
    "cells_for_node", "ontology_lookup",
    "retrieve_incidents",
    "build_handoff_pairs", "compute_metrics_for_pair", "compute_handoff_metrics",
    "aggregate_workflow", "aggregate_node",
    "red_nodes_from_diagnosis",
    "evaluate_hitl", "metrics_to_dict",
]
