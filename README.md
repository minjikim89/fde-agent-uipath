# FDE Agent: Pre-Flight Check for Enterprise AI Workflows

> **UiPath AgentHack, Track 2: Maestro BPMN**
> A BPMN-orchestrated agent that diagnoses *where an AI workflow will break*
> before it ships, routes high-risk findings to a human via UiPath Action Center,
> and emails an audit-trail report. **The thing we diagnose is a BPMN workflow
> with AI inside it; the thing we ship is a BPMN workflow that performs that
> diagnosis.** The diagnosis itself runs *as* a Maestro process. That isomorphism
> is the whole idea.

**License: MIT** (see [`LICENSE`](./LICENSE)). MIT is shown in the GitHub repo
**About** sidebar (license visibility is a submission hard-requirement).

---

## 1. What it is

`FDE Agent` is a **pre-mortem for agentic/AI workflows**. You hand it an
AI-augmented business process (BPMN 2.0 / Mermaid / image); it scores **every
node** on three risk axes (*general failure*, *security*, *handoff*) using a
proprietary mapping ontology (**v0.4, 36 cells**: 17 RED / 19 YELLOW × 3 axes)
grounded in **7,959 AIID incident vectors**, OWASP LLM Top 10 v2025, MITRE ATLAS
v5.4.0, and the MIT AI Risk/Mitigation taxonomy. Output: a **RED / AMBER / GREEN
heatmap**, multi-option mitigations per RED node, and an **EU AI Act / K-PIPA Art.
22-2 compliance trail**. When a handoff carries real risk, the run **stops and
asks a human** through UiPath Action Center before anything auto-approves.

**Who it is for.** A **CIO / CDO / CRO** who has to defend an AI deployment to a
board or a regulator. Their job-to-be-done: "prove this workflow won't fail in a
way I'll have to answer for." Today that means a Big-4 pre-deployment audit
(weeks, five figures). FDE Agent turns that into **one Maestro run**.

**Adoption beachhead (served today, not roadmap).** The reference vertical is
**Korean credit underwriting**: a working loan-diagnosis sample (11 nodes, 4 RED)
that *is* the EU AI Act **Annex III §5(b) credit-scoring** high-risk use case and
the K-PIPA **Art. 22-2** automated-decision disclosure obligation. This is a live,
regulated go-to-market channel through a Korean financial-services partner, not a
hypothetical: the loan flow runs unmodified on the same engine as the legal flow.

UiPath is the **execution, orchestration, and governance layer**. The shipped
demo runs a **deterministic in-process diagnosis engine** inside a governed UiPath
Coded Agent; multi-model brains (Claude, OpenAI) are **selectable via the BRAIN
config**, not invoked in the shipped demo. The CrewAI 5-role crew is the
**designed multi-agent architecture** (PoC: `scripts/agents/crew_poc.py`), a
selectable backend, not the path the demo dispatches at runtime (see §4). Whatever
backend a deployment selects, UiPath supplies the secret injection, the
orchestration boundary, and the human-in-the-loop gate around it.

## 2. Architecture: UiPath wraps the agent and the selectable backends

```
                ┌───────────────────────  UiPath Automation Cloud  ───────────────────────┐
                │                                                                          │
   BPMN / ──────┼─▶  Maestro BPMN process  (Studio Web: low-code canvas, the governance     │
  Mermaid /     │    spine: 18 flow objects / 20 sequence flows)                           │
   image        │       │                                                                  │
                │       ▼                                                                  │
                │   [Gateway_AI_Intervention]  graph.ai_intervention_nodes >= 1            │
                │       │ yes                                                              │
                │       ▼                                                                  │
                │   ┌─ Service Task ── "Start and wait for agent" ──────────────────────┐ │
                │   │  Coded Agent  fde-diagnosis-agent  (Python SDK, serverless,        │ │
                │   │  published to Orchestrator → Shared folder)                        │ │
                │   │  entry: coded_agent_wrapper.py:run(input) -> dict                  │ │
                │   │     └─ core.DiagnosisEngine  (ONE governed call computes all 3     │ │
                │   │        axes: general_failure / security / handoff)                 │ │
                │   │        shipped path: deterministic in-process engine.              │ │
                │   │        Designed backend (selectable): CrewAI 5-role crew +         │ │
                │   │        multi-model brains via Orchestrator creds (Claude /         │ │
                │   │        OpenAI) · metrics: IPS / ConfDecay / LaaJ                    │ │
                │   └────────────────────────────────────────────────────────────────────┘ │
                │       │  returns: hitl_required (bool), max_final_score, diagnoses[]      │
                │       ▼                                                                  │
                │   [Gateway_HITL_Required]  vars.hitl_required == true                    │
                │       │ true                          │ false                            │
                │       ▼                               ▼                                  │
                │   User Task                       heatmap + report                       │
                │   Task_ActionCenter_HITL  ◀── HUMAN IN THE LOOP                          │
                │   (Action Center: Approve /       │                                      │
                │    Modify / Reject the            ▼                                      │
                │    mitigation dossier)        RPA email + audit archive ─▶ End            │
                │       │ decision                                                         │
                │       └──▶ resume                                                        │
                │                                                                          │
                │   Orchestrator → Credentials  injects LLM/provider keys as env vars       │
                └──────────────────────────────────────────────────────────────────────────┘
```

The reasoning is **boxed inside a single governed Coded Agent**; the
**only** place state can change for a human is the Action Center User Task, reached
**only** when the gateway boolean says risk is real. That boundary (gateway →
Action Center) *is* the platform-usage story, and it is the hero moment below.

## 3. UiPath components used (depth over breadth)

> **Agent type: BOTH.** This solution uses a **Coded Agent**
> (`fde-diagnosis-agent`, Python SDK, serverless) **orchestrated by a low-code
> Maestro BPMN process** with a **low-code Action Center** human-in-the-loop task.
> The autonomous reasoning is the Coded Agent; the orchestration, gateways, and
> human gate are low-code on the UiPath canvas.

We deliberately run **deep** on three components and label the rest honestly as
*designed/extensible*, because the rubric prefers depth over a logo parade.

| Component | Role | Status | Coded / Low-code |
|---|---|---|---|
| **Studio Web + Maestro BPMN** | The diagnosis process *is* a Maestro BPMN 2.0 workflow on Automation Cloud (18 flow objects, 20 sequence flows) | **Used** | Low-code |
| **Coded Agent `fde-diagnosis-agent`** | Serverless Python-SDK agent published to Orchestrator → Shared; `coded_agent_wrapper.py:run()` computes all 3 axes in one governed call | **Used** | Coded |
| **Action Center (User Task)** | Human-in-the-loop review of the mitigation dossier (Approve / Modify / Reject), the gated decision point | **Used** | Low-code |
| **Orchestrator** | Hosts the Coded Agent (Shared folder), injects provider keys via Credentials, exposes the Tasks API for HITL task creation | **Used** | Coded + Low-code |
| Document Understanding (IDP) | BPMN-image / PDF node extraction for non-XML inputs | *Designed, extensible* | Low-code |
| RPA (email) | Final-report email + audit-trail archive on the GREEN/exit path | *Designed, extensible* | Low-code |

> Provider keys and Orchestrator credentials live in **Orchestrator → Credentials**
> and are injected to the Coded Agent runtime as env vars, never hard-coded.
> See [§5 Setup](#5-setup--run-judge-reproduction).

## 4. Coded vs low-code split (and one honest note on "multi-agent")

**The split.** The low-code Maestro canvas is the **governance spine**: start →
AI-intervention gateway → Coded Agent service task → HITL gateway → (Action Center
User Task | report) → RPA email → end. Every autonomous decision runs inside a
governed service task; the only human-mutating step is gated through Action Center.
The coded layer is the Python Coded Agent and its internals.

- **Coded** (Python SDK Coded Agent):
  - Diagnosis core: model-agnostic `core.DiagnosisEngine` (`scripts/core/`,
    `scripts/diagnose.py`). This is the **deterministic in-process engine the demo
    actually runs**.
  - CrewAI 5-role crew (`scripts/agents/crew_poc.py`): the **designed multi-agent
    architecture** and a **selectable backend**, not the shipped demo path. It is a
    PoC; the runtime `coded_agent_wrapper.py:run()` never dispatches it. When a
    deployment opts in, it runs **inside the governed UiPath layer** (external
    frameworks under UiPath governance are explicitly rewarded by the rules).
  - Handoff metrics (`scripts/metrics/`: IPS / ConfDecay / LaaJ).
  - Orchestrator client + Coded Agent entry (`scripts/uipath/uipath_client.py`,
    `coded_agent_wrapper.py`).
- **Low-code** (Maestro canvas): the 18-object BPMN, gateways, the Action Center
  User Task, and the RPA email step.

**Honest architecture note (what the demo runs vs. what is designed).** On the
canvas the runtime is **one governed Coded Agent**; `coded_agent_wrapper.py:run()`
calls the **deterministic in-process `DiagnosisEngine`** and computes all three
risk axes in a single call. A code review confirms the **CrewAI crew is never
dispatched at runtime**: `crew_runner.py` has zero callers, and `backend:'crewai'`
appears only as reported metadata, never as an executed path. The CrewAI 5-role /
9-role picture is therefore the **designed multi-agent architecture and the
conceptual diagram**, *not* nine separate service tasks fanning out across the
canvas. We do **not** claim "N Coded Agents fan out in parallel," and we do **not**
claim the crew runs in the demo. The platform-usage value is the **governance
boundary**: the UiPath Maestro gateway that reads `hitl_required`, the Action
Center User Task behind it, and the Orchestrator that injects credentials over the
Coded Agent. That boundary is real and demonstrated. See
`scripts/uipath/README.md` §3-a step 4 for the exact Studio binding.

## 5. Setup & run (judge reproduction)

**Core diagnosis — the gateway input (zero UiPath account, ~$0, no API keys):**

```bash
git clone https://github.com/minjikim89/fde-agent-uipath && cd fde-agent-uipath
pip install -r requirements.txt            # PyYAML (ontology); no heavy deps required
cd scripts && python3 uipath/coded_agent_wrapper.py loan
```

Expected, **reproducible out-of-the-box** (ontology-only / `degraded` mode, no corpus,
deterministic, <1s):
- **loan** = **3 RED** {N6, N7, N9}, `max_final_score ≈ 4.76`, **`hitl_required: true`**.
  This exact boolean is what `Gateway_HITL_Required` reads on the Maestro canvas.

> **Graceful degrade is the point, not a caveat.** With no AIID corpus the engine
> falls back to a hash-embed retrieval path and still scores, escalates, and fires
> the human gate (`hitl_required: true`) instead of crashing. A real failure-mode
> path, demonstrated, not narrated.
>
> **Full-corpus numbers** (`degraded:false`: loan = 4 RED {N4, N6, N7, N9},
> `max_final_score ≈ 4.88`, ConfDecay over-trust ≈ 21% on N6) require the AIID Chroma
> corpus (CC BY-SA 4.0) prepared locally; it is not redistributed (Share-Alike
> cascade avoidance). The gate fires in **both** modes; only the score granularity
> differs.

**UiPath credential check (secret-free output):**

```bash
cd scripts/uipath && python3 healthcheck.py     # validates token issuance only
# Credentials live in the OS keychain, never in .env:
#   uipath_base_url / uipath_client_id / uipath_client_secret / uipath_tenant
```

**Maestro import + bind + run:** full step-by-step (import the BPMN from
`scripts/uipath/bpmn_diagnosis_workflow.bpmn`, bind the Service Task via *Start and
wait for agent*, deploy the Action Center app, run the loan sample to the human gate)
in [`scripts/uipath/README.md`](./scripts/uipath/README.md) §3.

- Prereqs: UiPath Automation Cloud account with **Maestro** + **Action Center**
  enabled; provider keys (only if a model-backed brain is selected) registered in
  **Orchestrator → Credentials**.

## 6. The hero moment: silent over-trust, caught by the gateway

The failure this product exists to catch: a **low-confidence upstream LLM analysis
hands off to an over-confident downstream auto-approve step**, i.e. *silent
over-trust*. Nobody is lying; the upstream is uncertain and the downstream simply
doesn't read that uncertainty, so a bad decision sails through on autopilot.

Mechanically legible, not just thematic:

1. The Coded Agent's **ConfDecay** metric measures the confidence gap across each
   handoff. On the loan sample, node **N6** shows a **21% over-trust gap** (an
   upstream analysis ~21 points less confident than the downstream step treats it).
2. That drives `hitl_required = true` in the Coded Agent's return dict.
3. **`Gateway_HITL_Required` reads `vars.hitl_required == true` → routes TRUE.**
4. The run **stops at the `Task_ActionCenter_HITL` User Task**. A human consultant
   opens the dossier (the RED nodes + the multi-option mitigation table) and decides
   **Approve / Modify / Reject**.
5. The auto-approve path is **bypassed** because the risk was real; the run resumes
   only on the human decision.

HITL thresholds (any one trips the gate): `confdecay 0.2 / ips 0.5 / laaj 0.6 /
final_score 4.5`. The three handoff metrics, **IPS** (intent preservation),
**ConfDecay** (over-trust), and **LaaJ** (LLM-as-judge alignment), are the
exception/handoff-handling story this track rewards; ConfDecay is the one wired to
the gateway boolean for the demo.

> **Versatility (provable, tightly scoped):** demonstrated on **legal + Korean
> loan** on the same ontology-driven, BPMN-agnostic engine, two synthetic samples,
> no per-vertical code. The same pipeline extends to KYC-AML and procure-to-process,
> but we claim only what we run.

## 7. Built with **Claude Code** via *UiPath for Coding Agents* (Platform Usage bonus, +2)

> Per the AgentHack rules, solutions that use coding agents via *UiPath for Coding
> Agents* (Claude Code, Codex, Cursor, and others) earn **+2 Platform Usage points**
> when the usage is documented with **verifiable evidence** (which agent, how it
> contributed, and a prompt log / screenshot / dedicated README section). This is that
> dedicated section; the evidence is **self-contained in this repo** (the shipped
> integration code plus this section) and does not depend on the demo video.

**Which coding agent.** **Claude Code** (Anthropic's CLI), the primary engineering
agent across the build, integrated with UiPath via the *UiPath for Coding Agents*
workflow (`@uipath/cli`).

**How it contributed.** Claude Code authored and refactored the UiPath integration
that ships in this repo:

- **The UiPath integration layer (primary, self-contained evidence).** The entire
  `scripts/uipath/` integration: `uipath_client.py` (stdlib HTTP client, token cache,
  **Cloudflare-1010 User-Agent workaround** discovered and grounded against UiPath's
  docs), `coded_agent_wrapper.py`, `loop_b_symbolic.py`, `healthcheck.py`, and the
  BPMN 2.0 XML. All inspectable here; the +2 rests on this code.
- **The Coded Agent lifecycle.** The `uipath` CLI flow (`auth`, `init`, `pack`,
  `publish`) was driven via Claude Code; see `scripts/uipath/README.md` section 3-b.
- **Cross-document reconciliation.** Claim verification, MITRE ATLAS version
  correction (to **v5.4.0**), ontology cell re-count, retraction of the circular
  GraphRAG precision number, and a confidentiality scrub of organization codes and
  internal product names.

> **Repository history (disclosed).** This public repo is published with a **clean, curated history**; the development repo, with its multi-lane working history,
> is kept private. The +2 therefore does **not** rely on `git log`. It stands on this
> section and the shipped code above. Each published commit carries a
> `Co-Authored-By: Claude` trailer and a "Built with Claude Code via UiPath for Coding
> Agents" line.

**Evidence checkable in this repo (the rules accept any one; we provide three):**

1. This dedicated README section (explicitly accepted as evidence).
2. The `scripts/uipath/` **integration code**, authored by Claude Code, in this repo.
3. The published commit's `Co-Authored-By: Claude` trailer.

> Scoring note: full documentation + verifiable evidence = **+2**; partial = +1;
> undocumented = 0. Items 1 to 3 meet the full bar on their own, independent of the
> video.

## 8. Accuracy notes (pre-submission honesty)

- **GraphRAG retrieval precision was retracted** (circular gold set). The 8,287-node
  / 12,317-edge graph is a valid *structural* asset; no precision metric is claimed.
- The **handoff metrics** (IPS / ConfDecay / LaaJ) are real standalone modules.
  ConfDecay **is** wired to the gateway boolean for this demo (the hero moment);
  IPS / LaaJ are design/offline and presented as such, not as live-path claims.
- Phoenix/Arize observability is a **Rapid-track** layer; on this UiPath track the
  Sub-Agent 6 critic verdict is surfaced directly and escalated to Action Center.
- We do **not** claim a multi-agent fan-out on the canvas (see §4). One governed
  Coded Agent computes three axes; the governance boundary is the platform story.
- **The CrewAI crew does not run in the shipped demo.** A code review confirmed the
  Coded Agent always runs the deterministic in-process `DiagnosisEngine`;
  `crew_runner.py` has zero callers and `backend:'crewai'` is reported metadata only.
  CrewAI is the **designed multi-agent architecture** (`scripts/agents/crew_poc.py`)
  and a selectable backend, presented as such, not as a live-path claim.

## License & attribution

**MIT** ([`LICENSE`](./LICENSE); visible in the GitHub **About** sidebar). AIID
data is CC BY-SA 4.0 (not redistributed; attribution: AI Incident Database,
incidentdatabase.ai). UiPath organization code and sandbox credentials are
Confidential and are **not** present in this repo (`<ORG_CODE>` placeholder; real
values are Keychain-only).
