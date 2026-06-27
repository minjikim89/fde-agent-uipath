"""
FDE Agent — Diagnosis Engine v0.1 PoC

Pipeline:
  1. Parse Layer 2 workflow markdown → node inventory
  2. Load mapping ontology (9 cells, RED 3 nodes × 3 axes)
  3. Per RED node:
     a. Lookup ontology cells by axis
     b. Retrieve AIID similar incidents (Chroma top-5)
     c. Aggregate
  4. Render: heatmap (markdown table) + per-node dossier → report file

Import contract:
  - `from diagnose import red_nodes_from_diagnosis` is side-effect-free
    (no chroma/BGE-M3 load). All pipeline side effects live inside main().
"""
import yaml
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DATA = SCRIPT_DIR / 'data'
ONTOLOGY = DATA / 'mapping-ontology-v0.1.yaml'
CHROMA = DATA / 'chroma'
OUTPUT_DIR = SCRIPT_DIR / 'output'
OUTPUT_DIR.mkdir(exist_ok=True)

# v0.2 — Handoff Quantification Framework metric hooks (ontology v0.3)
sys.path.insert(0, str(SCRIPT_DIR))
from metrics.ips import compute_ips
from metrics.confdecay import compute_confdecay
from metrics.laaj import compute_laaj
from agents.aggregator import aggregate_workflow

SAMPLES_DIR = DATA / 'sample-workflows'
SAMPLE_MAP = {
    'legal': SAMPLES_DIR / 'legal-contract-review-v0.1.md',
    'loan': SAMPLES_DIR / 'loan-underwriting-kr-v0.1.md',
}

SAMPLE_TITLES = {
    'legal': 'Vendor Contract Review (Layer 2 sample v0.2)',
    'loan': 'Korean Personal Loan Underwriting (Layer 2 sample v0.2, AML+ACS)',
}

# Module-level state populated by main(); module-level function defs below close
# over these names. Stays None when the module is imported (e.g. by sub_agent_6
# for the red_nodes_from_diagnosis helper) so chroma / BGE-M3 are never loaded.
ontology: dict = {}
cells: list = []
model = None
inc_col = None


# =============================================================
# Public helpers (import-safe — no side effects)
# =============================================================

def red_nodes_from_diagnosis(diag_dict, threshold: float = 4.0) -> list:
    """Extract node IDs whose aggregated final_score >= threshold (default 4.0 = RED).

    diag_dict: {node_id: {'final_score': float, ...}, ...}
        Typical caller pattern:
            aggregated = aggregate_workflow(diagnoses, handoff_metrics_by_dn)
            red_ids = red_nodes_from_diagnosis({a.node_id: a.to_dict() for a in aggregated})

    Insertion order is preserved (Python dict ordering). Non-numeric/missing
    final_score values are treated as 0 (i.e. excluded).
    """
    out = []
    for nid, info in diag_dict.items():
        if not isinstance(info, dict):
            continue
        try:
            score = float(info.get('final_score', 0) or 0)
        except (TypeError, ValueError):
            continue
        if score >= threshold:
            out.append(nid)
    return out


def parse_workflow(md_path):
    with open(md_path) as f:
        content = f.read()
    nodes = []
    in_section = False
    for line in content.split('\n'):
        if '## Node Inventory' in line:
            in_section = True
            continue
        if in_section and line.startswith('## ') and 'Node Inventory' not in line:
            break
        if in_section and line.startswith('|') and '|' in line[1:]:
            parts = [p.strip() for p in line.split('|')[1:-1]]
            if len(parts) >= 4:
                node_label = parts[0]
                m = re.search(r'(N\w+)', node_label.replace('**', ''))
                if m and m.group(1) not in ('Node',):
                    function = parts[1]
                    ai_mode = parts[2]
                    prediction = parts[3]
                    color = 'RED' if 'RED' in prediction else ('YELLOW' if 'YELLOW' in prediction else 'GREEN')
                    nodes.append({
                        'id': m.group(1),
                        'function': function,
                        'ai_mode': ai_mode,
                        'predicted_color': color,
                        'prediction_text': prediction[:200],
                    })
    return nodes


def retrieve_aiid(query, n=5):
    """Side-effect: requires main() to have populated `model` and `inc_col`."""
    emb = model.encode([query], normalize_embeddings=True).tolist()[0]
    results = inc_col.query(query_embeddings=[emb], n_results=n)
    out = []
    for i in range(len(results['ids'][0])):
        out.append({
            'id': results['ids'][0][i],
            'title': results['metadatas'][0][i].get('title', ''),
            'similarity': 1 - results['distances'][0][i],
            'date': results['metadatas'][0][i].get('date', ''),
        })
    return out


def cells_for_node(node_id_simple, sample_source_filter=None):
    """Side-effect: requires main() to have populated module-level `cells`."""
    out = []
    for c in cells:
        cn = c.get('node', '')
        if cn.startswith(f'{node_id_simple.lower()}_') or cn.startswith(f'{node_id_simple}_'):
            if sample_source_filter:
                cs = c.get('sample_source', 'legal')  # default legal for v0.1 cells without sample_source
                if cs != sample_source_filter:
                    continue
            out.append(c)
    return out


def render_heatmap(nodes, sample_name):
    out = ['# FDE Agent — Diagnosis Report v0.1\n']
    out.append(f'**Workflow**: {SAMPLE_TITLES.get(sample_name, sample_name)}')
    out.append(f'**Diagnosed**: {time.strftime("%Y-%m-%d %H:%M")}\n')
    out.append('## Heatmap (per-node summary)\n')
    out.append('| Node | Function | AI Mode | Color | Predicted Risk |')
    out.append('|---|---|---|---|---|')
    for n in nodes:
        emoji = {'RED': '🔴', 'YELLOW': '🟡', 'GREEN': '🟢'}[n['predicted_color']]
        ai_mode_short = n['ai_mode'].replace('<i>', '').replace('</i>', '').replace('<br/>', ' ')[:60]
        out.append(f"| **{n['id']}** | {n['function'][:55]} | {ai_mode_short} | {emoji} | {n['predicted_color']} |")
    return '\n'.join(out)


def render_cell(cell):
    out = []
    axis = cell.get('axis', '?')

    if axis == 'general_failure':
        pf = cell.get('primary_failure_mode', 'N/A')
        out.append(f"- **Primary failure mode**: `{pf}`")
        secondary = cell.get('secondary_failure_modes', [])
        if secondary:
            out.append(f"- **Secondary**: {', '.join(secondary)}")
        out.append(f"- **Risk score**: {cell.get('risk_score', 'N/A')}")
        ev = cell.get('evidence', {}).get('aiid_incidents', [])
        if ev:
            ev_str = ', '.join('{} ({})'.format(e['id'], e['relevance']) for e in ev)
            out.append(f"- **Ontology evidence**: {ev_str}")
        ref = cell.get('mitigation_options_ref')
        if ref:
            out.append(f"- **Mitigation options**: see `{ref}`")

    elif axis == 'security':
        threats = cell.get('primary_threats', [])
        if threats:
            threats_str = ', '.join('{} {} ({})'.format(t['id'], t.get('title', ''), t.get('relevance', '')) for t in threats)
            out.append(f"- **OWASP threats**: {threats_str}")
        mitre = cell.get('mitre_atlas_techniques', cell.get('mitre_atlas_tactics', []))
        if mitre:
            mitre_str = ', '.join('{} {}'.format(m['id'], m.get('title', '')) for m in mitre)
            out.append(f"- **MITRE ATLAS**: {mitre_str}")
        out.append(f"- **Risk score**: {cell.get('risk_score', 'N/A')}")

    elif axis == 'handoff':
        out.append(f"- **Upstream dep**: {cell.get('upstream_dependency', 'N/A')}")
        out.append(f"- **Downstream**: {', '.join(cell.get('downstream_dependents', []))}")
        out.append(f"- **Primary handoff risk**: `{cell.get('primary_handoff_risk', 'N/A')}`")
        desc = cell.get('description', '').strip()
        if desc:
            out.append(f"- **Heuristic**: {desc[:200]}...")
        out.append(f"- **Risk score**: {cell.get('risk_score', 'N/A')}")
        out.append(f"- **Heuristic source**: {cell.get('heuristic_source', 'N/A')}")
        mit = cell.get('mitigation_options', {})
        if mit:
            out.append(f"- **Mitigation options (inline)**:")
            for k, v in mit.items():
                action = v.get('action', '')[:120]
                out.append(f"  - **{k}**: {action}")

    return '\n'.join(out)


def render_dossier(d):
    n = d['node']
    out = [f"\n## {n['id']}: {n['function'][:60]}\n"]
    out.append(f"- AI Mode: {n['ai_mode'][:80]}")
    out.append(f"- Predicted: {n['predicted_color']}\n")

    for axis in ['general_failure', 'security', 'handoff']:
        cells_in_axis = d['cells_by_axis'][axis]
        if cells_in_axis:
            out.append(f"### Axis: {axis}\n")
            for cell in cells_in_axis:
                out.append(render_cell(cell))
                out.append('')

    out.append('### Retrieved similar AIID incidents (top 5)\n')
    for inc in d['aiid'][:5]:
        out.append(f"- ({inc['similarity']:.3f}) **{inc['id']}**: {inc['title'][:100]} ({inc.get('date', '')})")

    return '\n'.join(out)


# ---------------------------------------------------------------------------
# v0.2 — Handoff Quantification Framework metric hooks (extend only, no refactor)
# ontology v0.3 §handoff_quantification_framework
# ---------------------------------------------------------------------------

def _bge_embed_fn(text):
    # BGE-M3 wrapper around the existing model object
    return model.encode([text], normalize_embeddings=True).tolist()[0]


def _synth_node_text(node, sample_source):
    parts = [node['id'], node['function'], node['ai_mode'], sample_source]
    return ' | '.join(p for p in parts if p)


def _synth_diagnosis_text(diagnosis):
    n = diagnosis['node']
    bits = [n['id'], n['function'], n['ai_mode']]
    for axis_name, cells in diagnosis['cells_by_axis'].items():
        for cell in cells:
            pf = cell.get('primary_failure_mode') or cell.get('primary_handoff_risk') or ''
            if pf:
                bits.append(f'{axis_name}:{pf}')
            for s in cell.get('secondary_failure_modes', []) or []:
                bits.append(s)
    for inc in diagnosis['aiid'][:3]:
        bits.append(inc['title'][:60])
    return ' | '.join(b for b in bits if b)


def _synth_confidence(diagnosis, role):
    # role: "upstream" -> LLM analytical node (낮은 conf realistic)
    #       "downstream" -> auto-decision (높은 conf — over-trust pattern instantiation)
    risk_scores = []
    for cells in diagnosis['cells_by_axis'].values():
        for c in cells:
            rs = c.get('risk_score')
            if isinstance(rs, (int, float)):
                risk_scores.append(rs)
    avg_risk = sum(risk_scores) / len(risk_scores) if risk_scores else 3.5
    if role == 'upstream':
        # high risk LLM = lower self-reported confidence (uncertainty acknowledged)
        # slope 0.15 (was 0.1): floor at 0.1 was exactly 0.80, but the over-trust
        # guard is strict `<0.8` → gap≡0 on every handoff. 0.15 lets high-risk
        # upstream dip below 0.8 (avg_risk 5.0→0.70) so over-trust actually fires.
        # Keep identical to core/tools.py:_synth_confidence.
        return round(max(0.55, min(0.85, 1.0 - (avg_risk - 3.0) * 0.15)), 2)
    else:
        # auto-decision downstream = artificially high (silent escalation signature)
        return round(max(0.85, min(0.99, 0.85 + (avg_risk - 3.0) * 0.05)), 2)


def _build_handoff_pairs(diagnoses):
    """diagnosis들의 handoff axis cells에서 upstream→current pair 추출.
       upstream_dependency가 list면 각 원소마다 pair 생성."""
    pairs = []
    diag_by_node = {d['node']['id']: d for d in diagnoses}
    for d in diagnoses:
        for cell in d['cells_by_axis']['handoff']:
            up_dep = cell.get('upstream_dependency')
            if not up_dep:
                continue
            ups = up_dep if isinstance(up_dep, list) else [up_dep]
            for up in ups:
                # upstream_dependency는 노드 라벨 (e.g., "N2_clause_extraction" or "loan_N6_llm_risk_analysis")
                up_short = re.match(r'(?:loan_)?(N\w+?)(?:_|$)', up)
                up_id = up_short.group(1) if up_short else up
                pairs.append({
                    'upstream_id': up_id,
                    'upstream_full': up,
                    'downstream_id': d['node']['id'],
                    'downstream_full': d['node']['function'][:50],
                    'cell': cell,
                    'downstream_diagnosis': d,
                    'upstream_diagnosis': diag_by_node.get(up_id),  # 있을 수도 없을 수도
                })
    return pairs


def _compute_metrics_for_pair(pair, sample_source):
    up_diag = pair['upstream_diagnosis']
    dn_diag = pair['downstream_diagnosis']
    dn_node = dn_diag['node']
    up_node_proxy = up_diag['node'] if up_diag else {
        'id': pair['upstream_id'],
        'function': pair['upstream_full'],
        'ai_mode': 'upstream (no RED diagnosis available)',
    }

    upstream_text = _synth_node_text(up_node_proxy, sample_source)
    downstream_text = _synth_diagnosis_text(dn_diag)

    ips_res = compute_ips(
        upstream_text, downstream_text,
        pair['upstream_id'], pair['downstream_id'],
        _bge_embed_fn,
    )

    # ConfDecay: upstream LLM = low-conf, downstream auto-decision = high-conf (silent escalation signature)
    up_conf = _synth_confidence(up_diag, 'upstream') if up_diag else 0.70
    dn_conf = _synth_confidence(dn_diag, 'downstream')
    cd_res = compute_confdecay(up_conf, dn_conf, pair['upstream_id'], pair['downstream_id'])

    laaj_ctx = {
        'workflow': sample_source,
        'handoff_pair': f"{pair['upstream_id']} → {pair['downstream_id']}",
        'ontology_handoff_risk': pair['cell'].get('primary_handoff_risk', ''),
        'expected_schema': pair['cell'].get('description', '')[:120],
    }
    laaj_res = compute_laaj(
        laaj_ctx,
        node_a={'id': pair['upstream_id'], 'type': 'upstream', 'output': upstream_text},
        node_b={'id': pair['downstream_id'], 'type': 'downstream', 'output': downstream_text},
        backend='mock',   # production은 'auto' → claude CLI 자동 (sampling 10% 권장)
    )
    return {'ips': ips_res, 'confdecay': cd_res, 'laaj': laaj_res, 'pair': pair}


def render_metrics_section(metric_rows, sample_name):
    out = ['\n---\n# Handoff Quantification Metrics (v0.3 framework)\n']
    out.append('Ontology §handoff_quantification_framework — runtime measurement on RED handoffs.')
    out.append(f'Embed backend: `BGE-M3 (normalize_embeddings=True)` | LaaJ backend: `mock` (production: `claude -p` w/ 10% sampling)\n')

    out.append('## IPS (Intent Preservation Score)')
    out.append('Threshold: ≥0.7 healthy · 0.5–0.7 watch · <0.5 Context Decay Alert\n')
    out.append('| Handoff | IPS | Band | Alert |')
    out.append('|---|---|---|---|')
    for r in metric_rows:
        out.append(r['ips'].to_row())

    out.append('\n## Confidence Decay')
    out.append('Over-Trust Gap = max(0, decay) when upstream<0.8 · Under-Use Gap = max(0,-decay) when upstream>0.9 · Alert >0.2\n')
    out.append('| Handoff | up.conf | dn.conf | Δ | gaps | Band | Alert |')
    out.append('|---|---|---|---|---|---|---|')
    for r in metric_rows:
        out.append(r['confdecay'].to_row())

    out.append('\n## LaaJ (LLM-as-a-Judge alignment)')
    out.append('Score <0.6 → manual_review_trigger · disagreement_flags non-empty → specific_issue · ≥0.8 trusted\n')
    out.append('| Handoff | score | Band | Alert | Top disagreement flag |')
    out.append('|---|---|---|---|---|')
    for r in metric_rows:
        out.append(r['laaj'].to_row())

    # Phoenix custom metric emission spec (Arize integration narrative)
    out.append('\n## Phoenix custom metric emission spec')
    out.append('Each row above maps 1:1 to a per-handoff span attribute set:')
    out.append('```')
    out.append('span.attributes["fde.handoff.ips"]               = ips.score')
    out.append('span.attributes["fde.handoff.ips_alert"]         = ips.alert')
    out.append('span.attributes["fde.handoff.confdecay"]         = confdecay.decay')
    out.append('span.attributes["fde.handoff.over_trust_gap"]    = confdecay.over_trust_gap')
    out.append('span.attributes["fde.handoff.under_use_gap"]     = confdecay.under_use_gap')
    out.append('span.attributes["fde.handoff.laaj_score"]        = laaj.alignment_score')
    out.append('span.attributes["fde.handoff.laaj_flags"]        = laaj.disagreement_flags')
    out.append('```')
    return '\n'.join(out)


def render_aggregated_section(aggregated):
    out = ['\n---\n# Aggregated Final Scores (Architecture §5 AGGREGATE)\n']
    out.append('Weights: handoff=0.4 (본인 IP moat) · security=0.3 · general=0.3 · runtime metric boost (handoff axis) up to +1.5')
    out.append('Color: ≥4.0 RED · 2.5–3.9 YELLOW · <2.5 GREEN\n')
    out.append('| Node | Final | Color | general | security | handoff_base | handoff+boost | Runtime alerts |')
    out.append('|---|---|---|---|---|---|---|---|')
    for a in aggregated:
        emoji = {'RED':'🔴','YELLOW':'🟡','GREEN':'🟢'}[a.color]
        ax = a.axis_scores
        alerts = '; '.join(a.runtime_metric_alerts[:2]) or '—'
        out.append(f"| **{a.node_id}** | {a.final_score:.2f} | {emoji} {a.color} | "
                   f"{ax['general_failure']:.2f} | {ax['security']:.2f} | "
                   f"{ax['handoff_base']:.2f} | {ax['handoff_with_boost']:.2f} | {alerts[:80]} |")

    out.append('\n## Per-node 3-element evidence cells (Heatmap design principle)\n')
    for a in aggregated:
        out.append(f"### {a.node_id} — final {a.final_score} {a.color}\n")
        for ev in a.evidence:
            refs = ', '.join(ev.evidence_refs[:3]) or '—'
            out.append(f"- **{ev.axis}** · failure_mode=`{ev.failure_mode}` · evidence_type=`{ev.evidence_type}` "
                       f"· refs=[{refs[:80]}] · mitigation=`{ev.mitigation_summary[:100]}`")
        out.append('')
    return '\n'.join(out)


# =============================================================
# CLI entry — all pipeline side effects live here
# =============================================================

def main():
    """Run the full diagnosis pipeline. CLI: python diagnose.py [sample-name]."""
    global ontology, cells, model, inc_col

    if len(sys.argv) > 1 and sys.argv[1] in SAMPLE_MAP:
        samples_to_run = [(sys.argv[1], SAMPLE_MAP[sys.argv[1]])]
    else:
        samples_to_run = list(SAMPLE_MAP.items())

    print('[1/5] Loading mapping ontology...')
    with open(ONTOLOGY) as f:
        ontology = yaml.safe_load(f)
    cells = ontology.get('cells', [])
    print(f'  loaded {len(cells)} cells')

    print(f'[2/5] Samples to process: {[name for name, _ in samples_to_run]}')

    print('[3/5] Loading Chroma + BGE-M3...')
    import chromadb
    from sentence_transformers import SentenceTransformer
    import torch
    device = 'mps' if torch.backends.mps.is_available() else 'cpu'
    t0 = time.time()
    model = SentenceTransformer('BAAI/bge-m3', device=device)
    client = chromadb.PersistentClient(path=str(CHROMA))
    inc_col = client.get_collection('aiid_incidents')
    print(f'  model + Chroma ready in {time.time()-t0:.1f}s')

    print('[4/5] Diagnosing RED nodes per sample...')
    all_results = []
    for sample_name, sample_path in samples_to_run:
        print(f'  === sample: {sample_name} ===')
        nodes = parse_workflow(sample_path)
        print(f'    parsed {len(nodes)} nodes')
        sample_source = 'korean_loan' if sample_name == 'loan' else 'legal'
        red_nodes = [n for n in nodes if n['predicted_color'] == 'RED']
        diagnoses = []
        for n in red_nodes:
            matched_cells = cells_for_node(n['id'], sample_source_filter=sample_source)
            by_axis = {'general_failure': [], 'security': [], 'handoff': []}
            for c in matched_cells:
                axis = c.get('axis')
                if axis in by_axis:
                    by_axis[axis].append(c)
            query = f"{n['function']} hallucination edge case failure"
            incidents = retrieve_aiid(query, n=5)
            diagnoses.append({
                'node': n,
                'cells_by_axis': by_axis,
                'aiid': incidents,
            })
            print(f'    {n["id"]}: {sum(len(v) for v in by_axis.values())} cells | {len(incidents)} AIID')
        all_results.append({
            'sample': sample_name,
            'sample_path': sample_path,
            'nodes': nodes,
            'diagnoses': diagnoses,
        })

    print('[5/5] Rendering reports...')

    print('[6/6] Computing handoff metrics per sample...')
    for result in all_results:
        sample_name = result['sample']
        nodes = result['nodes']
        diagnoses = result['diagnoses']
        sample_source = 'korean_loan' if sample_name == 'loan' else 'legal'

        pairs = _build_handoff_pairs(diagnoses)
        metric_rows = [_compute_metrics_for_pair(p, sample_source) for p in pairs]
        print(f'    {sample_name}: {len(pairs)} handoff pair(s), {sum(1 for r in metric_rows if r["ips"].alert)} IPS alert, '
              f'{sum(1 for r in metric_rows if r["confdecay"].alert)} ConfDecay alert, '
              f'{sum(1 for r in metric_rows if r["laaj"].alert)} LaaJ alert')

        # Aggregator hook (Step 4-bis) — per-node final risk score
        handoff_metrics_by_dn = {}
        for row in metric_rows:
            dn = row['pair']['downstream_id']
            handoff_metrics_by_dn.setdefault(dn, []).append(row)
        aggregated = aggregate_workflow(diagnoses, handoff_metrics_by_dn)
        n_red = sum(1 for a in aggregated if a.color == 'RED')
        print(f'    {sample_name}: aggregated {len(aggregated)} nodes ({n_red} RED final)')

        report = []
        report.append(render_heatmap(nodes, sample_name))
        report.append('\n---\n# RED Node Dossiers\n')
        for d in diagnoses:
            report.append(render_dossier(d))
        report.append(render_metrics_section(metric_rows, sample_name))
        report.append(render_aggregated_section(aggregated))

        output_path = OUTPUT_DIR / f'diagnosis-v0.2-{sample_name}.md'
        with open(output_path, 'w') as f:
            f.write('\n'.join(report))
        print(f'  {sample_name}: {output_path.name} ({output_path.stat().st_size / 1024:.1f} KB)')

    print('DONE')


if __name__ == '__main__':
    main()
