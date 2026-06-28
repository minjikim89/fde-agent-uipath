# FDE Agent — UiPath Integration Guide (Maestro BPMN, AgentHack Track 2)

> **Purpose**: the UiPath integration layer — Maestro BPMN asset, the Coded Agent
> wrapper, and the staging credential wiring.
> **Status**: token issuance PASS (reproducible via `healthcheck.py`). The Tasks API
> activates once the tenant name is set in the OS keychain.
> This guide is colocated with the code; the root [`README.md`](../../README.md) is
> the submission overview.

---

## 0. Asset manifest

| File | Role | UiPath account needed? |
|---|---|---|
| `bpmn_diagnosis_workflow.xml` / `.bpmn` | BPMN 2.0 — 9-step pipeline + explicit OR-join + BPMN DI layout. Import target for Maestro Studio | No (validate with xmllint + a bpmn.io validator) |
| `coded_agent_wrapper.py` | The UiPath Coded Agent wrapping the shared `core.DiagnosisEngine`. `run(input_payload) -> dict` standard entry point + multi-backend dispatch + **multi-model brain selection** (Claude / OpenAI via `brain_factory`) + **escalated-node HITL hook** + `submit_to_action_center()` helper | No (`python3 coded_agent_wrapper.py legal [brain]` runs as a sanity check) |
| `uipath.json` | Coded Agent **entry-point manifest** — `{ "functions": { "main": "coded_agent_wrapper.py:run" } }`. Read by `uipath init` | Yes (at publish) |
| `pyproject.toml` | Project metadata + deps. `description` must avoid `& < > " ' ;`; `authors` is required (packaging constraint) | Yes (at pack) |
| `loop_b_symbolic.py` | Loop B-Symbolic mitigation patch — stamps `fde:applied_options` + `<fde:mitigation>` into the BPMN XML (comment-preserving, UTC audit timestamp) | No (`python3 loop_b_symbolic.py --self-test`) |
| `hitl_mockup.html` | Action Center HITL form mockup. Visualizes the dossier review + WCAG 2.3.3 reduced-motion | No (open in a browser) |
| `uipath_client.py` | stdlib HTTP client — keychain creds + token cache (3600s, 60s margin) + mandatory User-Agent + Tasks API (list / create_generic_task / submit_diagnosis_for_hitl, dry-run by default) | Yes (real staging call at token issuance) |
| `healthcheck.py` | 3-stage credential validation (keychain → staging guard → token issuance). Never prints secret values | Yes (real token-endpoint call) |
| `README.md` | This document — step-by-step import guide | n/a |

---

## 0-bis. Environment wiring (MUST READ before any UiPath HTTP call)

### Environment (staging — NOT cloud.uipath.com)

- **Host**: `staging.uipath.com/<ORG_CODE>` (the organization code is confidential —
  read from keychain `uipath_base_url`, never committed in plaintext).
- `uipath_client.UiPathClient` **rejects immediately** with `UiPathConfigError` if it
  finds `cloud.uipath.com` in the base_url (guards against production misuse).

### Cloudflare 1010 workaround — User-Agent header required

UiPath staging sits behind Cloudflare, and **any request without a browser-style
User-Agent is rejected with HTTP 403 + `error code: 1010`**.

- `uipath_client.DEFAULT_USER_AGENT` = a standard desktop Chrome UA string.
- It is force-wrapped onto every `urllib.request.Request`. If you fall back to `curl`,
  pass `-A "..."` explicitly.

### Keychain entries (credentials — never store in plaintext or logs)

| service | Role |
|---|---|
| `uipath_base_url` | staging Orchestrator base URL |
| `uipath_client_id` | client_credentials grant client ID |
| `uipath_client_secret` | client_credentials grant secret |
| `uipath_tenant` | Orchestrator tenant name (required for the Tasks API) |

Set an entry with:
```bash
security add-generic-password -s uipath_tenant -a key -w "<TENANT_NAME>"
```

`uipath_client._keychain_read` uses only `security find-generic-password -s <service> -w`
(no account-name branching). A missing entry is blocked up front with `UiPathConfigError`.

### Token flow

```
POST {base_url}/identity_/connect/token
Content-Type: application/x-www-form-urlencoded
User-Agent:   <browser-style>     # required, or you get a 1010
Body: grant_type=client_credentials&client_id=…&client_secret=…&scope=<space-separated>
```

Default scopes (12): `OR.Tasks` × {base, .Read, .Write}, `OR.Execution` × 3,
`OR.Folders` × 3, `OR.Jobs` × 3.

Token cache: process-lifetime in-memory, with an `expires_in - 60s` safety margin.

### Healthcheck — reproduce credential validity

```bash
cd scripts/uipath
python3 healthcheck.py            # human-readable
python3 healthcheck.py --json     # 1-line JSON for pipes/cron
```

Expected output (token issuance succeeds, tenant not yet set):

```
[1] Keychain entries (presence only):
   ✓ uipath_base_url
   ✓ uipath_client_id
   ✓ uipath_client_secret
   ✗ uipath_tenant
[2] Staging guard: ok / host: https://<netloc only — org path stripped>
[3] Token issuance: ok / Bearer / expires_in_sec ~3599 / scopes (count) 12
[overall]: ok
[tenant]: tenant entry missing — set keychain 'uipath_tenant' once confirmed
```

Secret values never leak to stdout / stderr / exception messages at any stage. The
**organization code (`/<ORG_CODE>`) and tenant name are also stripped** from healthcheck
output — only the netloc is shown.

### Tasks API (Action Center HITL) — dry-run by default

```python
from uipath_client import UiPathClient
client = UiPathClient()

# Tenant gated — raises UiPathConfigError until the 'uipath_tenant' keychain entry is set
tasks = client.list_tasks(top=5)

# Dry-run by default — returns the request payload + endpoint, no network call
preview = client.create_generic_task(
    title="[FDE] loan-uw diagnosis",
    priority="High",
    data={"hitl_reason": "...", "diagnoses": [...]},
    folder_id=42,
)

# Or via the convenience helper (maps a coded_agent_wrapper.run() result)
hitl = client.submit_diagnosis_for_hitl(
    diagnosis_result=run_result,   # from coded_agent_wrapper.run()
    bpmn_workflow_id="loan-uw:v0.1",
)
# preview / hitl: {"dry_run": True, "method": "POST", "endpoint": "...", "request_body": {...}}
```

Actual submission (`dry_run=False`) is intentionally opt-in.

### End-to-end flow

```
coded_agent_wrapper.run(payload)        # diagnosis + parsed metrics + hitl_required
        │
        ├──► (HITL=True) loop_b_symbolic.apply_mitigation(node, option_id)   # BPMN patch (XML)
        │
        └──► coded_agent_wrapper.submit_to_action_center(run_result, dry_run=True)
                   │
                   └──► UiPathClient.submit_diagnosis_for_hitl
                               │
                               └──► UiPathClient.create_generic_task
                                           │
                                           └──► POST {base}/{tenant}/orchestrator_/tasks/GenericTasks/CreateTask
```

The final step makes a real network call only with `dry_run=False` and the tenant set.

---

## 1. Pre-pack validation (sanity without a UiPath account)

### 1.1 BPMN XML validator

Confirm OMG schema conformance with an online BPMN 2.0 validator.

```bash
# Option A: bpmn.io online validator (browser)
open https://bpmn.io/toolkit/bpmn-js/walkthrough/
# top-right "Open Diagram" → drop bpmn_diagnosis_workflow.xml

# Option B: Camunda Modeler (desktop) — File → Open
# Option C: xmllint (well-formedness only, no schema validation)
xmllint --noout scripts/uipath/bpmn_diagnosis_workflow.xml
```

Expected: well-formed + 18 flow objects (start ×1 + service tasks ×9 + user task ×1 +
exclusive gateways ×3 [AI-intervention branch + Gateway_HITL_Required + Gateway_HITL_Join]
+ parallel gateways ×2 + end events ×2) + 20 sequence flows.

### 1.2 Coded Agent wrapper sanity

```bash
cd scripts   # from the repo root
python uipath/coded_agent_wrapper.py legal
python uipath/coded_agent_wrapper.py loan
```

Expected output (JSON to stdout; ontology-only / `degraded` mode without the corpus):

```json
{
  "status": "ok",
  "sample_name": "loan",
  "diagnoses": [
    {"node_id": "N6", "score": 4.24, "color": "RED"},
    {"node_id": "N7", "score": 4.76, "color": "RED"},
    {"node_id": "N9", "score": 4.41, "color": "RED"}
  ],
  "max_final_score": 4.76,
  "hitl_required": true,
  "hitl_reason": "aggregator.max_final_score >= 4.5 ; ..."
}
```

This `hitl_required` boolean is exactly what the Maestro Exclusive Gateway
`Gateway_HITL_Required` reads in its `conditionExpression`. (With the full AIID corpus
prepared locally the loan sample reports 4 RED and `max_final_score ≈ 4.88`; the gate
fires in both modes.)

### 1.3 HITL mockup capture

```bash
open scripts/uipath/hitl_mockup.html
```

Capture notes:
- 1920×1080 full screen
- confirm the left dossier panel fits one screen without scrolling
- the alert-bar `pulse` animation captured in slow motion
- Approve button hover → click sequence

---

## 2. Maestro BPMN learning checklist

- [ ] UiPath Maestro docs: https://docs.uipath.com/maestro
- [ ] BPMN 2.0 vs the UiPath dialect — especially `userTask` (Action Center) + `serviceTask` (Coded Agent) mapping
- [ ] Coded Agent Python SDK: https://docs.uipath.com/agents/automation-cloud/latest/user-guide/about-coded-agents
- [ ] Action Center custom form designer (form-builder schema)

---

## 3. Import step-by-step

### 3-a. Maestro BPMN import

```
1) UiPath Automation Cloud → left rail Maestro "Start modeling", or Studio Web →
   New Project → project type "Agentic Process" (the official name).
   To model without an account: the bpmn.uipath.com sandbox canvas.
2) Designer top-right "Import from file" → drop bpmn_diagnosis_workflow.xml
   (the reverse "Download to file" exports the canvas back to .bpmn)
3) Studio applies layout automatically (DI coordinates preserved) — confirm all
   18 flow objects are visible.
4) Model-vs-code reality: after the Sprint 7 refactor the core is a single
   coded_agent_wrapper.py:run (parse → risk×3 → RAG → mitigation → aggregate, all
   in-process). The 9 service tasks are NOT split into 5 separate agents. The honest
   end-to-end demo binds one Agent task ("FDE Diagnosis") to the published Coded Agent,
   plus a User task (HITL) and an RPA email task.
   Binding: select the task → toolbox "Change element" → "Service task" →
   Implementation: Action = "Start and wait for agent" → pick the published
   fde-diagnosis-agent from the Automation dropdown → map inputs
   (sample_name / workflow_content) via Variable search.
   (The 9-task diagram is the conceptual architecture for the deck.)
   - Task_RPA_Email: Service task → Action = "Start and wait for RPA workflow" (Outlook/Gmail)
5) User Task Task_ActionCenter_HITL: "Change element" → "User task" →
   Action = "Create action app task" → select the action app from §3-c + a Task title +
   input mapping (escalated_nodes / diagnoses)
6) Gateway_HITL_Required: select the Exclusive gateway after the agent task → expand
   Conditions → "Open expression editor" → `vars.hitl_required == true` (or
   `vars.max_final_score >= 4.5`). Maps to the run() dict's hitl_required /
   max_final_score keys. Declare variables via the start-event Arguments "Add new".
```

### 3-b. Coded Agent upload + secret manager

> Verified flow (UiPath Python SDK official CLI — https://uipath.github.io/uipath-python/cli/ ,
> https://pypi.org/project/uipath/ ). Note: an earlier `npm install -g @uipath/cli` +
> `uipath package new --type coded-agent --entrypoint ...` was **wrong** — the entry
> point is declared in `uipath.json` and read by `uipath init`.

```
1) Install the UiPath Python SDK (Coded Agent SDK + CLI):
   pip install uipath

2) Publish the Coded Agent (auth → init → pack → publish):
   cd scripts/uipath
   uipath auth                      # browser OAuth → .env (org URL + PAT)
   uipath init                      # uipath.json functions → entry-points.json
   uipath pack                      # build the .nupkg
   uipath publish --folder Shared   # upload to the Orchestrator feed
   # entry point = uipath.json: { "functions": { "main": "coded_agent_wrapper.py:run" } }

3) Register LLM provider keys in the Secret Manager (never in plaintext):
   In UiPath Orchestrator → Tenant → Credentials:
     - ANTHROPIC_API_KEY (Claude primary brain)
   These are injected into the Coded Agent runtime as env vars — never hard-coded.

4) Coded Agent runtime dependencies (only for the full-corpus path):
   in pyproject.toml or requirements.txt:
     chromadb >= 0.5
     sentence-transformers >= 2.7
     torch >= 2.3
     pyyaml >= 6.0
   The AIID Chroma vector DB is mounted separately (Orchestrator "Storage Buckets",
   or fetched on first run). Without it the engine runs in degraded ontology-only mode.
```

### 3-c. Action Center HITL form deploy

```
1) Action Center → Action App → New → "FDE HITL Review"
2) Form template:
   - Option A (recommended): rebuild from hitl_mockup.html in the form builder.
     If the form builder does not embed native HTML/CSS, decompose into widgets:
       · Header: Text widget (workflow + assignee meta)
       · Alert bar: Banner widget (color=red, bound to confdecay/ips/laaj vars)
       · Dossier panel: read-only JSON viewer + AIID citation list widget
       · Mitigation options: repeating panel × 3 (must / recommend / optional)
                              + 5-row trade-off table widget + checkbox
       · K-PIPA box: disclosure widget
       · Audit preview: code-block widget
       · Action buttons: Submit (Approve) + Reject + Modify
   - Option B (simple): render the Markdown report + only the option selection as widgets
3) Form variable binding:
   in:  diagnosis_payload (the coded_agent_wrapper.run() dict)
   out: approved_options (list of "must_fix" | "recommend" | "optional")
        + approver_id + action ("approve" | "modify" | "reject")
4) Routing rule: auto-assign to a reviewer group (e.g. "Risk Management"), SLA 4h
```

### 3-d. RPA email step setup

```
1) Task_RPA_Email service task → Email Send activity
2) From: the project maintainer (business address)
3) To: <client_email> (bound from the input payload)
4) Subject: "[FDE Agent] {sample_name} workflow diagnosis — {max_final_score} {color}"
5) Attachments (report_paths from the coded_agent_wrapper return dict):
   - heatmap_html
   - executive_summary_md
   - optional: side-by-side-diff.html (after Loop B-Symbolic)
6) Body template:
   - mandatory K-PIPA Art. 22-2 footer
   - audit-trail unique ID (Maestro instance ID)
   - next-step (consulting upsell CTA)
```

### 3-e. Smoke test (end-to-end)

```
1) Maestro Studio "Run" → upload the loan sample markdown
2) Track in Action Center:
   - Parse step ~30s
   - Diagnosis (3 axes) ~3min (includes Chroma load)
   - AIID RAG step ~30s
   - Mitigation ~20s
   - HITL gate → confirm routing to Action Center
3) From a reviewer account, open the HITL form → check "Must Fix + Recommend" → Approve
4) Confirm heatmap render + RPA email sent
5) Audit trail: Orchestrator → Logs → FDE_Diagnosis_Process
```

---

## 4. Video capture plan (Devpost 5-min — hero moment ~1:00–2:30)

| Beat | Source | Capture asset in this directory |
|---|---|---|
| (a) Setup | Maestro BPMN canvas (loan-uw:v0.1) | Maestro Studio screen after importing bpmn_diagnosis_workflow.xml |
| (b) Trigger | metric panel | hitl_mockup.html top alert-bar (pulse animation) |
| (c) Routing | Maestro Exclusive Gateway zoom | zoom Gateway_HITL_Required in the BPMN + conditionExpression overlay |
| (d) Dossier | RED node N7 dossier panel | hitl_mockup.html left panel, full screen |
| (e) Resolution | Approve → re-deploy → side-by-side | hitl_mockup.html right options → button click → side-by-side diff |

---

## 5. Change history (UiPath layer)

- **v0.1 (Phase 2 pre-pack)**: new `scripts/uipath/` directory — BPMN 2.0 + DI,
  `coded_agent_wrapper.py` (read-only wrap of the diagnosis core + HITL gate evaluator),
  `hitl_mockup.html` (Action Center HITL form mockup), this guide.
- **v0.2 (Sprint 3 polish + Loop B-Symbolic)**: BPMN `Gateway_HITL_Required` ↔
  conditionExpression alignment, explicit `Gateway_HITL_Join` OR-join, DI updates; HITL
  mockup `prefers-reduced-motion` (WCAG 2.3.3); `coded_agent_wrapper.py` 3-tier dispatch
  (CrewAI primary / LangGraph fallback / direct) + strict payload validation; new
  `loop_b_symbolic.py` (comment-preserving BPMN mitigation patch with UTC audit timestamp).
- **v0.3 (staging credential wire)**: new `uipath_client.py` (stdlib HTTP client, token
  cache, mandatory User-Agent CF-1010 bypass, dry-run Tasks API, staging-only guard) and
  `healthcheck.py` (3-stage credential validation, secret-free output); `submit_to_action_center`
  helper added.
- **v0.4 (Coded Agent publish + multi-model brain + HITL hook)**: new `uipath.json`
  (entry-point manifest) and `pyproject.toml`; `coded_agent_wrapper.py` gains multi-model
  brain selection (Claude / OpenAI via `brain_factory`),
  escalated-node HITL hook, and the corrected `uipath init` flow.
