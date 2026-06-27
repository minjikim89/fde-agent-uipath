"""
FDE Agent — Sub-Agent 6: Peer Reviewer / Self-Critique (v0.2, 4-way dispatch)

Architecture.md §2 Sub-Agent 6 정합 + § Model Policy 분기 (2026-05-29 closure):
  - Role : Cross-check Sub-Agent 2 (Risk Diagnosis) output — red-team / alternative view
  - Out  : disagreement_flags + alternative_view + peer_confidence (0~1)
  - Phoenix custom metric hook (signature only; wiring is 🅓)
  - Meta : "We diagnose AI workflows. Our own diagnosis is self-diagnosed."

Backend dispatch (BRAIN_PEER env var) — 해커톤별 권장 분기:
  - "gemini"           → VertexGeminiBrain (Vertex AI + ADC, google-genai SDK)
                          ★ Rapid Agent 제출 경로 고정 — "Google Cloud AI tools" 규정 정합.
                          같은 Gemini 계열 (Pro critic vs primary Flash) adversarial 2nd-pass.
                          Cloud Run runtime service account가 ADC 자동 충족. No API key.
                          BRAIN_PEER_GEMINI_MODEL env로 critic 모델 override.
  - "gemini_ai_studio" → GeminiBrain (Google AI Studio API, Keychain key)
                          ★ Ablation/non-Rapid 컨텍스트만. Rapid 제출에선 사용 금지.
  - "claude"           → subprocess `claude -p` (UiPath 권장; 구독 우선)
  - "vertex"           → Vertex AI Model Garden Claude adapter (UiPath fallback)
  - "mock"             → deterministic logic (e2e dry-run / unit test)
  - "auto"             → fail-safe chain: claude → vertex → gemini → mock (UiPath multi-model)

⚠️ Rapid 제출 경로: BRAIN_PEER=gemini 고정 (=VertexGeminiBrain, ADC).
   Claude/Vertex-Claude/AI Studio key 호출 금지 (규정 위반 또는 secret 의존성).
⚠️ Claude Code session 내부: BRAIN_PEER=claude / auto 금지 (subprocess 재귀).
   상세 운영 규칙은 `peer_review_prompt.md § Operational rules — BRAIN_PEER env` 참조.

Trigger semantics:
  peer_confidence < 0.6              → alert=True (LLM disagreement)
  disagreement_flags non-empty       → alert=True (specific issue)
  else                               → alert=False

Phoenix span attribute keys (signature only — actual emission in 🅓):
  fde.peer.confidence
  fde.peer.alert
  fde.peer.flags
  fde.peer.axis.alignment
  fde.peer.axis.coverage
  fde.peer.axis.hallucination_risk
  fde.peer.backend  (one of: gemini | gemini_ai_studio | claude | vertex | mock)
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any


# =============================================================
# Constants & paths
# =============================================================

PROMPT_PATH = Path(__file__).parent / "peer_review_prompt.md"

_log = logging.getLogger(__name__)


def _rapid_pinned() -> bool:
    """Rapid 제출 경로 Gemini-only pin. Opt-in (default '0') — 공유 코드베이스라
    UiPath/dev multi-model 을 깨지 않도록 비활성 기본. Rapid Cloud Run deploy 가
    FDE_RAPID=1 을 명시 주입할 때만 비-Gemini backend 를 차단한다."""
    return os.environ.get("FDE_RAPID", "0").lower() in ("1", "true", "yes")


# Rapid pin 시 기본 backend = gemini (Vertex ADC). 비-Rapid(FDE_RAPID=0)는 claude 기본 유지.
DEFAULT_BACKEND = os.environ.get(
    "BRAIN_PEER", "gemini" if _rapid_pinned() else "claude"
).lower()

# Backend dispatch behaviour:
#   * Per-backend subprocess timeout (claude / vertex / gemini). 10s default —
#     keeps the Sub-Agent 6 cost cap predictable when looping over RED nodes.
#   * Sticky cache TTL applies to (workflow, node_id, backend) result cache;
#     review_workflow reuses entries within the TTL to skip duplicate LLM calls.
DEFAULT_BACKEND_TIMEOUT_S = int(os.environ.get("BRAIN_PEER_TIMEOUT", "10"))
DEFAULT_CACHE_TTL_S = int(os.environ.get("BRAIN_PEER_CACHE_TTL", "300"))

# Gemini critic model override. When unset (None), GeminiBrain falls back to
# brain_factory.DEFAULT_GEMINI_MODEL at invocation time — no read happens here.
# Architecture § Model Policy: "Pro critic vs primary Flash" → operators set
# BRAIN_PEER_GEMINI_MODEL to a Pro variant for the canonical self-critique.
DEFAULT_GEMINI_PEER_MODEL = os.environ.get("BRAIN_PEER_GEMINI_MODEL")

# Trigger thresholds
PEER_CONF_ALERT_THRESHOLD = 0.6
PEER_CONF_TRUSTED_THRESHOLD = 0.8

AXES = ("alignment", "coverage", "hallucination_risk")


# =============================================================
# Data classes
# =============================================================

@dataclass
class PeerReviewResult:
    """
    Single-node peer review result. Aggregator + heatmap renderer가 duck-type field만 보면 됨.
    """
    node_id: str
    sample_source: str                  # "legal" or "korean_loan"
    backend: str                        # "gemini" | "gemini_ai_studio" | "claude" | "vertex" | "mock"
    peer_confidence: float              # 0~1
    axis_scores: dict                   # {alignment, coverage, hallucination_risk} each 0~5
    alternative_view: str
    disagreement_flags: list[str] = field(default_factory=list)
    alert: bool = False                 # peer_confidence < 0.6 OR flags non-empty
    latency_ms: float = 0.0
    error: str | None = None            # set if backend failed (still returns mock fallback)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> str:
        """Markdown row helper — diagnosis-v0.3 disagreement section 정합."""
        emoji = "⚠️" if self.alert else "✅"
        flag_str = "; ".join(self.disagreement_flags[:2])[:140] if self.disagreement_flags else "—"
        return (
            f"| **{self.node_id}** | {self.sample_source} | {self.peer_confidence:.2f} | "
            f"{emoji} | {self.axis_scores.get('alignment', 0)} / "
            f"{self.axis_scores.get('coverage', 0)} / "
            f"{self.axis_scores.get('hallucination_risk', 0)} | {flag_str} |"
        )

    def phoenix_attributes(self) -> dict:
        """Phoenix custom metric emission spec — Phase 1 🅒 wire 시 직접 호출."""
        return {
            "fde.peer.confidence": self.peer_confidence,
            "fde.peer.alert": self.alert,
            "fde.peer.flags": list(self.disagreement_flags),
            "fde.peer.axis.alignment": self.axis_scores.get("alignment", 0),
            "fde.peer.axis.coverage": self.axis_scores.get("coverage", 0),
            "fde.peer.axis.hallucination_risk": self.axis_scores.get("hallucination_risk", 0),
            "fde.peer.backend": self.backend,
            "fde.peer.latency_ms": self.latency_ms,
        }


# =============================================================
# Public API
# =============================================================

class SubAgent6PeerReviewer:
    """
    Single-pass peer reviewer / self-critique.
    diagnose.py의 `diagnosis` dict + sample_source → PeerReviewResult.

    backend:
      - "gemini"           → VertexGeminiBrain (Vertex AI + ADC) self-critique
                              ★ Rapid Agent submission path — "Google Cloud AI tools" 정합
      - "gemini_ai_studio" → GeminiBrain (AI Studio Keychain key) — ablation only
      - "claude"           → `claude -p` subprocess (UiPath multi-model)
      - "vertex"           → Vertex AI Model Garden Claude adapter (UiPath fallback)
      - "mock"             → deterministic logic (no LLM call)
      - "auto"             → fail-safe chain: claude → vertex → gemini → mock (UiPath)

    Result cache (class-level, shared across instances within a process):
      key   = (workflow_name, node_id, requested_backend)
      value = (timestamp, PeerReviewResult)
      TTL   = BRAIN_PEER_CACHE_TTL env (seconds, default 300)
    Sticky backend memo (auto-mode only):
      key   = workflow_name
      value = backend that first succeeded for this workflow
    """

    # class-level cache — shared across instances in a single process
    _result_cache: dict = {}
    _sticky_backend: dict = {}

    def __init__(self, backend: str | None = None, prompt_path: Path | None = None,
                 vertex_project: str | None = None, vertex_region: str = "us-east5",
                 vertex_model: str = "claude-opus-4-7",
                 gemini_model: str | None = None,
                 timeout_s: int | None = None, cache_ttl_s: int | None = None):
        self.backend = (backend or DEFAULT_BACKEND).lower()
        self.prompt_path = prompt_path or PROMPT_PATH
        self.vertex_project = vertex_project or os.environ.get("GCP_PROJECT")
        self.vertex_region = vertex_region
        self.vertex_model = vertex_model
        # gemini critic model — explicit arg > BRAIN_PEER_GEMINI_MODEL env > None
        # (None lets GeminiBrain pick its own default model when invoked)
        self.gemini_model = gemini_model or DEFAULT_GEMINI_PEER_MODEL
        # per-backend timeout (subprocess) + sticky cache TTL
        self.timeout_s = timeout_s if timeout_s is not None else DEFAULT_BACKEND_TIMEOUT_S
        self.cache_ttl_s = cache_ttl_s if cache_ttl_s is not None else DEFAULT_CACHE_TTL_S
        # lazy holders
        self._prompt_cache: str | None = None
        # Separate slots so Vertex and AI Studio brains don't share init state.
        # `gemini` (Rapid default) → _vertex_gemini_brain
        # `gemini_ai_studio` (ablation) → _ai_studio_gemini_brain
        self._vertex_gemini_brain = None
        self._ai_studio_gemini_brain = None

    # ---------- result cache helpers ----------

    def _cache_get(self, workflow: str, node_id: str, backend: str):
        key = (workflow, node_id, backend)
        entry = self._result_cache.get(key)
        if not entry:
            return None
        ts, result = entry
        if time.time() - ts > self.cache_ttl_s:
            self._result_cache.pop(key, None)
            return None
        return result

    def _cache_put(self, workflow: str, node_id: str, backend: str, result) -> None:
        self._result_cache[(workflow, node_id, backend)] = (time.time(), result)

    @classmethod
    def cache_clear(cls) -> None:
        """Drop all cached peer-review results + sticky backend memo. Tests / fresh runs."""
        cls._result_cache.clear()
        cls._sticky_backend.clear()

    # ---------- public ----------

    def review_node(self, diagnosis: dict, sample_source: str,
                    ontology_version: str = "v0.4") -> PeerReviewResult:
        """
        diagnosis: diagnose.py에서 만든 dict {'node':..., 'cells_by_axis':{...}, 'aiid':[...]}
        sample_source: "legal" | "korean_loan"
        ontology_version: for prompt context only

        Cache: result is cached for `self.cache_ttl_s` seconds keyed by
            (sample_source, node_id, requested_backend). Within the TTL the
            cached PeerReviewResult is returned without invoking any backend.
        Sticky: in "auto" mode, the first non-mock backend that succeeds for a
            workflow is reused for subsequent nodes in the same workflow
            (skipping the claude → vertex → gemini → mock retry chain).
        """
        node_id = diagnosis["node"]["id"]

        # Sticky override (auto mode only): if a prior node in this workflow
        # picked a concrete backend, route through that one first.
        requested_backend = self.backend
        backend_override = None
        if requested_backend == "auto":
            sticky = self._sticky_backend.get(sample_source)
            if sticky:
                backend_override = sticky

        # Cache lookup — key uses the *requested* backend (user-visible label)
        cached = self._cache_get(sample_source, node_id, requested_backend)
        if cached is not None:
            return cached

        t0 = time.time()
        prompt_payload = self._build_input(diagnosis, sample_source, ontology_version)
        # mock backend는 raw cells (nested regulatory anchor 포함)을 그대로 사용,
        # 실제 LLM backend는 summarized prompt_payload만 (token cost)
        backend, raw_json, err = self._dispatch(
            prompt_payload, raw_diagnosis=diagnosis, backend_override=backend_override,
        )
        latency = round((time.time() - t0) * 1000, 1)

        parsed = self._parse_response(raw_json, fallback_diagnosis=diagnosis, sample_source=sample_source)
        alert = (
            parsed["peer_confidence"] < PEER_CONF_ALERT_THRESHOLD
            or bool(parsed["disagreement_flags"])
        )
        result = PeerReviewResult(
            node_id=node_id,
            sample_source=sample_source,
            backend=backend,
            peer_confidence=parsed["peer_confidence"],
            axis_scores=parsed["axis_scores"],
            alternative_view=parsed["alternative_view"],
            disagreement_flags=parsed["disagreement_flags"],
            alert=alert,
            latency_ms=latency,
            error=err,
        )

        # Sticky memo: record first non-mock success for auto-mode workflows
        if (
            requested_backend == "auto"
            and not err
            and backend != "mock"
            and sample_source not in self._sticky_backend
        ):
            self._sticky_backend[sample_source] = backend

        self._cache_put(sample_source, node_id, requested_backend, result)
        return result

    def review_workflow(self, diagnoses: list, sample_source: str,
                        ontology_version: str = "v0.4") -> list[PeerReviewResult]:
        """Per-RED-node review. diagnose.py의 all_results[i]['diagnoses']와 1:1.

        Sticky cache: results are memoized per (workflow, node_id, backend) so
        a re-invocation within `BRAIN_PEER_CACHE_TTL` returns the prior
        PeerReviewResult without re-hitting the backend.
        """
        return [self.review_node(d, sample_source, ontology_version) for d in diagnoses]

    # ---------- input shaping ----------

    def _build_input(self, diagnosis: dict, sample_source: str, ontology_version: str) -> dict:
        """diagnosis dict → peer-review prompt input JSON (matches prompt schema)."""
        n = diagnosis["node"]
        # axis-level cell summarization (drop verbose fields, keep peer-relevant)
        cells_by_axis_summary = {}
        for axis, cells in diagnosis.get("cells_by_axis", {}).items():
            row = []
            for c in cells:
                item: dict[str, Any] = {
                    "cell_id": c.get("cell_id", "unknown"),
                    "risk_score": c.get("risk_score"),
                }
                if axis == "general_failure":
                    item["primary_failure_mode"] = c.get("primary_failure_mode")
                    item["evidence_refs"] = [
                        f"{e.get('id')} ({e.get('relevance', '')})"
                        for e in (c.get("evidence", {}) or {}).get("aiid_incidents", [])
                    ]
                elif axis == "security":
                    item["primary_threats"] = [t.get("id") for t in (c.get("primary_threats", []) or [])]
                elif axis == "handoff":
                    item["primary_handoff_risk"] = c.get("primary_handoff_risk")
                    item["heuristic_source"] = c.get("heuristic_source")
                row.append(item)
            cells_by_axis_summary[axis] = row

        # AIID top-5 summary
        aiid = []
        for inc in diagnosis.get("aiid", [])[:5]:
            aiid.append({
                "id": inc.get("id"),
                "title": (inc.get("title") or "")[:120],
                "similarity": round(float(inc.get("similarity") or 0.0), 3),
            })

        # aggregate_risk = mean of all cell risk_scores
        all_scores = []
        for cells in diagnosis.get("cells_by_axis", {}).values():
            for c in cells:
                rs = c.get("risk_score")
                if isinstance(rs, (int, float)):
                    all_scores.append(float(rs))
        agg = round(sum(all_scores) / len(all_scores), 2) if all_scores else 0.0
        predicted_color = "RED" if agg >= 4.0 else ("YELLOW" if agg >= 2.5 else "GREEN")

        return {
            "context": {
                "workflow": sample_source,
                "ontology_version": ontology_version,
                "regulatory_anchors": self._anchors_for(sample_source),
            },
            "primary_diagnosis": {
                "node_id": n["id"],
                "function": n.get("function", ""),
                "ai_mode": n.get("ai_mode", ""),
                "predicted_color": predicted_color,
                "aggregate_risk": agg,
                "cells_by_axis": cells_by_axis_summary,
                "aiid_incidents": aiid,
            },
        }

    @staticmethod
    def _anchors_for(sample_source: str) -> list[str]:
        if sample_source == "korean_loan":
            return [
                "EU AI Act Annex III Point 5(b) Creditworthiness",
                "NIST AI RMF 1.0 + GenAI Profile",
                "K-PIPA Art 22-2 (자동결정 거부권)",
                "공정대출법", "신용정보법", "KoFIU AML",
            ]
        return [
            "EU AI Act Annex III (High-Risk AI Systems)",
            "NIST AI RMF 1.0 + GenAI Profile",
            "OWASP LLM Top 10 v2025",
            "MITRE ATLAS",
        ]

    # ---------- backend dispatch ----------

    def _dispatch(self, payload: dict, raw_diagnosis: dict | None = None,
                  backend_override: str | None = None) -> tuple[str, str, str | None]:
        """
        Returns (backend_used, raw_response_text, error_if_any).
        On any backend failure (timeout, missing creds, parse error), returns mock fallback.

        raw_diagnosis: full diagnosis dict (including nested regulatory anchors) used by mock backend.
                       Real LLM backends only see the summarized prompt payload (token cost).
        backend_override: route this single call through a specific backend (sticky-mode bypass).
                       Used by review_node when the workflow already has a sticky backend memo;
                       does not mutate self.backend.
        """
        backend = (backend_override or self.backend).lower()
        # Rapid pin (FDE_RAPID=1): 비-Gemini backend 요청을 gemini(Vertex ADC)로 강제 치환.
        # claude / vertex(=Model Garden Claude) / gemini_ai_studio / auto 전 경로 봉인.
        if _rapid_pinned() and backend not in ("gemini", "mock"):
            _log.warning(
                "FDE_RAPID pin: peer-review backend %r coerced to 'gemini' "
                "(non-Gemini AI tools not permitted on Rapid submission path)",
                backend,
            )
            backend = "gemini"
        prompt_text = self._load_prompt()
        user_block = json.dumps(payload, ensure_ascii=False, indent=2)
        workflow = payload["context"]["workflow"]
        mock_diag = raw_diagnosis if raw_diagnosis else self._payload_to_pseudo_diagnosis(payload)

        if backend == "mock":
            return "mock", self._mock_response_from(mock_diag, workflow), None

        # Explicit gemini path (Rapid 제출 경로) — Vertex AI + ADC. Runs first only
        # when explicitly requested. In auto-mode, gemini is tried *after*
        # claude/vertex to preserve the subscription-first principle for UiPath.
        if backend == "gemini":
            ok, raw, err = self._call_gemini(prompt_text, user_block, use_vertex=True)
            if ok:
                return "gemini", raw, None
            return "mock", self._mock_response_from(mock_diag, workflow), err

        # AI Studio Gemini — ablation only. Not for Rapid submission (regulation).
        if backend == "gemini_ai_studio":
            ok, raw, err = self._call_gemini(prompt_text, user_block, use_vertex=False)
            if ok:
                return "gemini_ai_studio", raw, None
            return "mock", self._mock_response_from(mock_diag, workflow), err

        if backend in ("claude", "auto"):
            ok, raw, err = self._call_claude(prompt_text, user_block, timeout_s=self.timeout_s)
            if ok:
                return "claude", raw, None
            if backend == "claude":
                return "mock", self._mock_response_from(mock_diag, workflow), err

        if backend in ("vertex", "auto"):
            ok, raw, err = self._call_vertex(prompt_text, user_block, payload)
            if ok:
                return "vertex", raw, None
            if backend == "vertex":
                return "mock", self._mock_response_from(mock_diag, workflow), err

        if backend == "auto":
            # Vertex Gemini in auto-chain (not AI Studio — secret-free preference)
            ok, raw, err = self._call_gemini(prompt_text, user_block, use_vertex=True)
            if ok:
                return "gemini", raw, None

        # auto → all three concrete backends failed
        return "mock", self._mock_response_from(mock_diag, workflow), "all backends unavailable; mock fallback"

    def _load_prompt(self) -> str:
        if self._prompt_cache is None:
            self._prompt_cache = self.prompt_path.read_text(encoding="utf-8")
        return self._prompt_cache

    # ---------- gemini (Vertex AI default / AI Studio ablation) — Rapid self-critique ----------

    def _call_gemini(self, system_prompt: str, user_block: str,
                     use_vertex: bool = True) -> tuple[bool, str, str | None]:
        """
        Invoke Gemini as the self-critique peer reviewer.

        use_vertex=True (default, Rapid Agent path):
            brain_factory.VertexGeminiBrain — Vertex AI + ADC, no API key.
            Cloud Run runtime SA satisfies ADC; local needs `gcloud auth
            application-default login` + GOOGLE_CLOUD_PROJECT/LOCATION envs.
        use_vertex=False (ablation / non-Rapid contexts):
            brain_factory.GeminiBrain — legacy AI Studio path, Keychain key.
            Not for Rapid submission (regulation: AI Studio key ≠ "Google
            Cloud AI tools").

        Same Gemini family adversarial 2nd-pass per architecture.md § Model
        Policy. Self-consistency / Reflexion technique alignment.

        Returns (ok, raw_text, error). On any failure returns (False, "", err)
        for _dispatch to mock-fall-back. Does *not* mutate brain_factory state —
        only imports the brain class.
        """
        if use_vertex:
            return self._invoke_brain(
                holder_attr="_vertex_gemini_brain",
                class_name="VertexGeminiBrain",
                ready_via_healthcheck=True,
                system_prompt=system_prompt,
                user_block=user_block,
            )
        return self._invoke_brain(
            holder_attr="_ai_studio_gemini_brain",
            class_name="GeminiBrain",
            ready_via_healthcheck=False,
            system_prompt=system_prompt,
            user_block=user_block,
        )

    def _invoke_brain(self, *, holder_attr: str, class_name: str,
                      ready_via_healthcheck: bool,
                      system_prompt: str, user_block: str) -> tuple[bool, str, str | None]:
        """Shared invocation path for brain_factory backed brains.

        holder_attr: attribute name on self that caches the brain instance.
        class_name: brain class to import from brain_factory.
        ready_via_healthcheck: VertexGeminiBrain exposes `healthcheck()['ready']`
            (no separate `ready()` method per C lane contract). GeminiBrain
            keeps the older `ready()` method. We branch on this flag.
        """
        try:
            mod = __import__("brain_factory", fromlist=[class_name])
            brain_cls = getattr(mod, class_name)
        except (ImportError, AttributeError) as e:
            return False, "", f"brain_factory.{class_name} import failed: {type(e).__name__}: {e}"

        brain = getattr(self, holder_attr)
        if brain is None:
            try:
                if class_name == "GeminiBrain" and self.gemini_model:
                    brain = brain_cls(model=self.gemini_model)
                elif class_name == "VertexGeminiBrain" and self.gemini_model:
                    brain = brain_cls(model=self.gemini_model)
                else:
                    brain = brain_cls()
                setattr(self, holder_attr, brain)
            except Exception as e:
                return False, "", f"{class_name} init error: {type(e).__name__}: {e}"

        # Readiness check
        if ready_via_healthcheck:
            hc = brain.healthcheck()
            if not hc.get("ready"):
                # Build a precise diagnostic from healthcheck dict (no secrets)
                diag = ", ".join(
                    f"{k}={v}" for k, v in hc.items()
                    if k in ("sdk_installed", "project_env_set", "location_env_set", "vertex_routing")
                )
                return False, "", f"{class_name} not ready: {diag}"
        else:
            if not brain.ready():
                hc = brain.healthcheck()
                return False, "", (
                    f"{class_name} not ready: "
                    f"api_key={'set' if hc.get('api_key_present') else 'MISSING'}, "
                    f"sdk={'installed' if hc.get('sdk_installed') else 'MISSING'}"
                )

        full_prompt = (
            f"{system_prompt}\n\n---\n\n## INPUT\n\n"
            f"```json\n{user_block}\n```\n\n"
            "Return the JSON object exactly per schema."
        )
        try:
            text = brain.generate(full_prompt)
            return True, text, None
        except Exception as e:
            return False, "", f"{class_name.lower()} error: {type(e).__name__}: {e}"

    # ---------- claude -p subprocess ----------

    @staticmethod
    def _call_claude(system_prompt: str, user_block: str,
                     timeout_s: int = DEFAULT_BACKEND_TIMEOUT_S) -> tuple[bool, str, str | None]:
        """
        Invoke `claude -p` subprocess (구독 우선 — Max plan reuse).
        Returns (ok, raw_text, error).
        timeout_s: per-call wall clock (default DEFAULT_BACKEND_TIMEOUT_S = BRAIN_PEER_TIMEOUT env, 10s).
        """
        if shutil.which("claude") is None:
            return False, "", "claude CLI not on PATH"

        # `claude -p` accepts the prompt via stdin (long-form) or argv (short).
        full_input = f"{system_prompt}\n\n---\n\n## INPUT\n\n```json\n{user_block}\n```\n\nReturn the JSON object exactly per schema."
        try:
            proc = subprocess.run(
                ["claude", "-p", "--output-format", "text"],
                input=full_input,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            if proc.returncode != 0:
                return False, proc.stdout, f"claude exit={proc.returncode}: {proc.stderr[:200]}"
            return True, proc.stdout, None
        except subprocess.TimeoutExpired:
            return False, "", "claude subprocess timeout"
        except Exception as e:
            return False, "", f"claude subprocess error: {type(e).__name__}: {e}"

    # ---------- vertex ai model garden (stub) ----------

    def _call_vertex(self, system_prompt: str, user_block: str, payload: dict) -> tuple[bool, str, str | None]:
        """
        Vertex AI Model Garden Claude adapter.

        Real impl (when GCP cred + anthropic[vertex] SDK 도착):
          from anthropic import AnthropicVertex
          client = AnthropicVertex(region=self.vertex_region, project_id=self.vertex_project)
          msg = client.messages.create(
              model=self.vertex_model,
              max_tokens=1024,
              system=system_prompt,
              messages=[{"role": "user", "content": f"INPUT:\n{user_block}"}],
          )
          return True, msg.content[0].text, None

        Current: stub — try import, fail gracefully.
        """
        try:
            from anthropic import AnthropicVertex  # type: ignore
        except ImportError:
            return False, "", "anthropic[vertex] SDK not installed (pip install 'anthropic[vertex]')"

        if not self.vertex_project:
            return False, "", "GCP_PROJECT env var not set"

        try:
            client = AnthropicVertex(region=self.vertex_region, project_id=self.vertex_project)
            msg = client.messages.create(
                model=self.vertex_model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": f"INPUT:\n{user_block}"}],
            )
            return True, msg.content[0].text, None
        except Exception as e:
            return False, "", f"vertex error: {type(e).__name__}: {e}"

    # ---------- response parsing ----------

    @staticmethod
    def _parse_response(raw: str, fallback_diagnosis: dict, sample_source: str) -> dict:
        """
        Extract strict JSON from LLM response. If parse fails, fall back to mock-equivalent
        deterministic output so the e2e pipeline never crashes.
        """
        # Strip markdown code fences
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                pc = float(obj.get("peer_confidence", 0.0))
                axes = obj.get("axis_scores", {}) or {}
                axes = {a: max(0, min(5, int(axes.get(a) or 0))) for a in AXES}
                return {
                    "peer_confidence": round(max(0.0, min(1.0, pc)), 2),
                    "axis_scores": axes,
                    "alternative_view": (obj.get("alternative_view") or "").strip(),
                    "disagreement_flags": [
                        str(f).strip()[:240] for f in (obj.get("disagreement_flags") or [])[:5]
                    ],
                }
            except (json.JSONDecodeError, ValueError):
                pass
        # fallback: mock equivalent
        mock_raw = SubAgent6PeerReviewer._mock_response_from(fallback_diagnosis, sample_source)
        return SubAgent6PeerReviewer._parse_response(mock_raw, fallback_diagnosis, sample_source) \
            if mock_raw else {
                "peer_confidence": 0.0,
                "axis_scores": {a: 0 for a in AXES},
                "alternative_view": "(parse failure; no peer view available)",
                "disagreement_flags": ["peer review backend returned unparseable output"],
            }

    # ---------- mock backend (deterministic) ----------

    @staticmethod
    def _payload_to_pseudo_diagnosis(payload: dict) -> dict:
        """Re-shape prompt payload → diagnosis-like dict for mock logic."""
        pd = payload["primary_diagnosis"]
        cells_by_axis = {}
        for axis, items in pd.get("cells_by_axis", {}).items():
            cells_by_axis[axis] = items
        return {
            "node": {"id": pd["node_id"], "function": pd["function"], "ai_mode": pd["ai_mode"]},
            "cells_by_axis": cells_by_axis,
            "aiid": pd.get("aiid_incidents", []),
            "_sample_source": payload["context"]["workflow"],
        }

    @staticmethod
    def _mock_response_from(diagnosis: dict, sample_source: str) -> str:
        """
        Deterministic peer review logic.

        Rubric application (mock):
          alignment:        cell count balance + risk_score consistency with predicted color
          coverage:         all 3 axes have ≥1 cell + regulatory_anchors expected per workflow
          hallucination_risk: AIID top-5 similarity stats (mean, max)

        Special cases (calibration anchors):
          - legal N3 borderline → flag security thinness + AIID weak (peer_confidence ≈ 0.53)
          - loan N9 KYC → flag missing handoff axis if absent
          - loan N7 silent escalation → concur (peer_confidence ≈ 0.87)
        """
        n = diagnosis["node"]
        nid = n.get("id", "")
        cells_by_axis = diagnosis.get("cells_by_axis", {}) or {}
        aiid = diagnosis.get("aiid", [])

        # --- 1. Alignment: do cell risk_scores corroborate the predicted color?
        all_scores = []
        for cells in cells_by_axis.values():
            for c in cells:
                rs = c.get("risk_score")
                if isinstance(rs, (int, float)):
                    all_scores.append(float(rs))
        agg = sum(all_scores) / len(all_scores) if all_scores else 0.0
        # alignment heuristic: tight spread + high mean → 5; wide spread → 3; missing → 0
        if not all_scores:
            alignment = 0
        else:
            spread = max(all_scores) - min(all_scores)
            if agg >= 4.5 and spread <= 0.8:
                alignment = 5
            elif agg >= 4.0:
                alignment = 4 if spread <= 1.0 else 3
            elif agg >= 3.5:
                alignment = 3
            else:
                alignment = 2

        # --- 2. Coverage: all 3 axes present + regulatory anchors
        axis_present = {a: bool(cells_by_axis.get(a)) for a in ("general_failure", "security", "handoff")}
        n_axes = sum(axis_present.values())
        coverage = {3: 5, 2: 3, 1: 1, 0: 0}[n_axes]

        # workflow-specific regulatory expectations
        regulatory_flags = []
        if sample_source == "korean_loan":
            # K-PIPA / 공정대출법 / KoFIU should appear in heuristic_source or evidence
            blob = json.dumps(cells_by_axis, ensure_ascii=False)
            has_kr_reg = any(kw in blob for kw in ("K-PIPA", "KoFIU", "공정대출법", "신용정보법"))
            if not has_kr_reg:
                coverage = max(1, coverage - 1)
                regulatory_flags.append(
                    "K-PIPA / 공정대출법 / KoFIU 규제 anchor가 cells에 명시되지 않음 — korean_loan workflow 필수 citation."
                )

        # --- 3. Hallucination risk: AIID similarity stats
        if aiid:
            sims = [float(i.get("similarity", 0.0)) for i in aiid]
            mean_sim = sum(sims) / len(sims)
            max_sim = max(sims)
            if mean_sim >= 0.7 and max_sim >= 0.85:
                hallucination = 5
            elif mean_sim >= 0.55:
                hallucination = 4
            elif mean_sim >= 0.40:
                hallucination = 3
            elif mean_sim >= 0.25:
                hallucination = 2
            else:
                hallucination = 1
        else:
            hallucination = 2  # no RAG evidence — borderline

        # --- Disagreement flags (specific)
        flags: list[str] = []

        # axis-thinness flags
        for axis, has in axis_present.items():
            if not has:
                if axis == "security":
                    flags.append(f"Security axis missing for {nid} — expected OWASP threat citation.")
                elif axis == "handoff":
                    flags.append(f"Handoff axis missing for {nid} — boundary risk implicit.")
                elif axis == "general_failure":
                    flags.append(f"general_failure axis missing for {nid} — primary failure mode unclassified.")

        # security thinness (single OWASP threat for RED)
        sec_cells = cells_by_axis.get("security", []) or []
        for c in sec_cells:
            threats = c.get("primary_threats", []) or []
            risk = c.get("risk_score", 0) or 0
            if isinstance(risk, (int, float)) and risk >= 4.0 and len(threats) <= 1:
                # threats may be list[dict] (raw ontology) or list[str] (summarized payload)
                first = threats[0] if threats else None
                threat_id = first.get("id") if isinstance(first, dict) else (first or "(none)")
                flags.append(
                    f"Security axis cites only {threat_id} for {nid} — expected ≥2 OWASP threats given risk_score={risk}."
                )
                break

        # AIID weak retrieval
        if aiid:
            sims = [float(i.get("similarity", 0.0)) for i in aiid]
            if sims and (sum(sims) / len(sims)) < 0.55:
                flags.append(
                    f"AIID top-5 mean similarity {sum(sims)/len(sims):.2f} — RAG evidence suggestive, not corroborative."
                )

        # regulatory anchor flags
        flags.extend(regulatory_flags)

        # borderline RED check (aggregate 4.0~4.15) — legal N3 pattern
        if 4.0 <= agg <= 4.15 and len(sec_cells) <= 1:
            flags.append(
                f"Verdict RED borderline for {nid} (agg={agg:.2f}); design-time base may be YELLOW absent runtime metric boost."
            )

        # cap at 5
        flags = flags[:5]

        # --- peer_confidence
        peer_conf = round((alignment + coverage + hallucination) / 3 / 5, 2)

        # --- alternative view (templated)
        alt = SubAgent6PeerReviewer._templated_alt_view(
            nid=nid, sample_source=sample_source, agg=agg,
            axis_present=axis_present, flags=flags,
        )

        out = {
            "peer_confidence": peer_conf,
            "axis_scores": {
                "alignment": alignment,
                "coverage": coverage,
                "hallucination_risk": hallucination,
            },
            "alternative_view": alt,
            "disagreement_flags": flags,
        }
        return json.dumps(out, ensure_ascii=False)

    @staticmethod
    def _templated_alt_view(nid: str, sample_source: str, agg: float,
                            axis_present: dict, flags: list[str]) -> str:
        if not flags and agg >= 4.0:
            return "Concur with primary; minor reservations only — see flags."
        bits = []
        if sample_source == "korean_loan":
            bits.append(
                f"{nid}: Korean loan vertical — peer reviewer expects explicit K-PIPA Art 22-2 + 공정대출법 anchors on handoff axis."
            )
        else:
            bits.append(
                f"{nid}: legal vertical — peer reviewer expects EU AI Act high-risk citation if contract decision is auto-executed."
            )
        if not axis_present.get("handoff"):
            bits.append("Handoff axis absent — primary IP differentiator (handoff heuristic) not instantiated for this node.")
        if not axis_present.get("security"):
            bits.append("Security axis absent — OWASP/MITRE ATLAS mapping not surfaced.")
        if 4.0 <= agg <= 4.15:
            bits.append("Verdict sits on RED/YELLOW boundary — runtime metric boost is doing the work; design-time evidence is YELLOW-ish.")
        return " ".join(bits[:3])


# =============================================================
# Render helpers — diagnosis-v0.3 disagreement section
# =============================================================

def render_peer_section(reviews: list[PeerReviewResult]) -> str:
    """diagnosis-v0.3-{legal,loan}.md의 Sub-Agent 6 disagreement section."""
    out = []
    out.append("\n---\n# Sub-Agent 6 — Claude Peer Reviewer (Multi-LLM cross-check)\n")
    out.append(
        "Meta narrative: *We diagnose AI workflows. Our own diagnosis is cross-checked by an ensemble of Gemini + Claude.*\n"
    )
    out.append(
        f"Backend: `{reviews[0].backend if reviews else 'mock'}` (dispatch: BRAIN_PEER env var; "
        "gemini=VertexGeminiBrain ADC ★ Rapid 고정 / gemini_ai_studio=AI Studio key (ablation) / "
        "claude=`claude -p` subprocess 구독 우선 / vertex=Vertex AI Model Garden Claude / "
        "mock=deterministic)\n"
    )
    out.append("Trigger thresholds: `peer_confidence < 0.6` OR `disagreement_flags non-empty` → ALERT (Phoenix `fde.peer.alert=True`)\n")

    # Summary table
    out.append("## Peer review summary (per RED node)\n")
    out.append("| Node | Workflow | peer_confidence | Alert | align / cov / halluc | Top disagreement flag |")
    out.append("|---|---|---|---|---|---|")
    for r in reviews:
        out.append(r.to_row())

    # Detailed alternative views
    out.append("\n## Alternative views + disagreement flags (full)\n")
    for r in reviews:
        emoji = "⚠️" if r.alert else "✅"
        out.append(f"### {r.node_id} — peer_confidence {r.peer_confidence:.2f} {emoji}\n")
        out.append(f"**Axis scores** (0~5): alignment={r.axis_scores.get('alignment', 0)} · "
                   f"coverage={r.axis_scores.get('coverage', 0)} · "
                   f"hallucination_risk={r.axis_scores.get('hallucination_risk', 0)}")
        out.append(f"\n**Alternative view**: {r.alternative_view}\n")
        if r.disagreement_flags:
            out.append("**Disagreement flags**:")
            for f in r.disagreement_flags:
                out.append(f"- {f}")
            out.append("")
        else:
            out.append("_No disagreement flags — peer concurs._\n")
        if r.error:
            out.append(f"_Backend note: {r.error}_\n")

    # Phoenix emission spec
    out.append("\n## Phoenix custom metric emission spec (Sub-Agent 6)\n")
    out.append("Each row above maps 1:1 to per-node span attributes:\n")
    out.append("```python")
    out.append('span.set_attribute("fde.peer.confidence",          peer.peer_confidence)')
    out.append('span.set_attribute("fde.peer.alert",               peer.alert)')
    out.append('span.set_attribute("fde.peer.flags",               peer.disagreement_flags)')
    out.append('span.set_attribute("fde.peer.axis.alignment",      peer.axis_scores["alignment"])')
    out.append('span.set_attribute("fde.peer.axis.coverage",       peer.axis_scores["coverage"])')
    out.append('span.set_attribute("fde.peer.axis.hallucination_risk", peer.axis_scores["hallucination_risk"])')
    out.append('span.set_attribute("fde.peer.backend",             peer.backend)')
    out.append('span.set_attribute("fde.peer.latency_ms",          peer.latency_ms)')
    out.append("```")
    return "\n".join(out)


# =============================================================
# CLI / smoke test
# =============================================================

# =============================================================
# e2e demo runner (no new script — CLI sub-mode on this module)
#
# Usage:
#   python sub_agent_6_peer_review.py                     # smoke test
#   python sub_agent_6_peer_review.py demo legal          # → diagnosis-v0.3-legal.md
#   python sub_agent_6_peer_review.py demo loan           # → diagnosis-v0.3-loan.md
#   python sub_agent_6_peer_review.py demo all            # both
# =============================================================

def _red_node_specs_from_v02(md_path: Path, threshold: float = 4.0) -> list[tuple[str, str, str]]:
    """Parse diagnosis-v0.2-{sample}.md → [(node_id, function, ai_mode), ...] for RED nodes
    (aggregated final_score >= threshold).

    Single source of RED-set truth: delegates the threshold filter to
    `diagnose.red_nodes_from_diagnosis` so the helper, the aggregator output,
    and this demo agree on what "RED" means.

    Parses two tables from the v0.2 report:
      * Heatmap section          → {node_id: (function, ai_mode)}
      * Aggregated Final Scores  → {node_id: {'final_score': float}}
    """
    text = md_path.read_text(encoding="utf-8")
    node_meta: dict[str, tuple[str, str]] = {}
    diag_dict: dict[str, dict] = {}

    in_heatmap = False
    in_agg = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## Heatmap"):
            in_heatmap, in_agg = True, False
            continue
        if "Aggregated Final Scores" in stripped and stripped.startswith("#"):
            in_heatmap, in_agg = False, True
            continue
        # Any other section header / horizontal rule closes the current block
        if stripped.startswith("## ") or stripped.startswith("# ") or stripped == "---":
            in_heatmap = False
            in_agg = False
            continue
        if not line.startswith("| **N"):
            continue
        cols = [c.strip().strip("*").strip() for c in line.split("|")[1:-1]]
        if len(cols) < 3:
            continue
        nid = cols[0]
        if in_heatmap:
            # cols: [node, function, ai_mode, predicted_color]
            node_meta[nid] = (cols[1], cols[2])
        elif in_agg:
            # cols: [node, predicted, final, aggregated, ...]
            try:
                diag_dict[nid] = {"final_score": float(cols[2])}
            except ValueError:
                continue

    # Delegate threshold filter to the single-source helper in diagnose.py
    # (import-safe since diagnose.py wraps pipeline side effects in main()).
    import sys as _sys
    SCRIPTS = Path(__file__).resolve().parent.parent
    if str(SCRIPTS) not in _sys.path:
        _sys.path.insert(0, str(SCRIPTS))
    from diagnose import red_nodes_from_diagnosis
    red_ids = red_nodes_from_diagnosis(diag_dict, threshold=threshold)

    out: list[tuple[str, str, str]] = []
    for nid in red_ids:
        fn, mode = node_meta.get(nid, ("(unknown)", "(unknown)"))
        out.append((nid, fn, mode))
    return out


def _e2e_demo(sample: str, backend: str = "mock") -> Path:
    """
    Read ontology + v0.2 diagnosis markdown → run Sub-Agent 6 peer review on RED nodes
    → write diagnosis-v0.3-{sample}.md (v0.2 content + Sub-Agent 6 section appended).

    Notes
    -----
    - AIID retrieve is deferred to Phase 1 wire (current PoC uses ontology evidence.aiid_incidents
      field as peer-review hallucination_risk input — sufficient for v0.3 demo).
    - RED node selection: aggregated final_score >= 4.0 (helper-driven, not hardcoded).
    """
    import yaml

    SCRIPT_DIR = Path(__file__).parent.parent
    ONTOLOGY = SCRIPT_DIR / "data" / "mapping-ontology-v0.1.yaml"
    OUTPUT_DIR = SCRIPT_DIR / "output"

    v02_path = OUTPUT_DIR / f"diagnosis-v0.2-{sample}.md"
    if not v02_path.exists():
        raise FileNotFoundError(f"v0.2 base not found: {v02_path}")

    v02_content = v02_path.read_text(encoding="utf-8")

    sample_source = "korean_loan" if sample == "loan" else "legal"

    # Load ontology cells
    ontology = yaml.safe_load(ONTOLOGY.read_text(encoding="utf-8"))
    cells = ontology.get("cells", []) or []
    ontology_version = ontology.get("version", "v0.4")

    # RED node specs derived from diagnosis-v0.2-{sample}.md aggregated table
    # via the diagnose.red_nodes_from_diagnosis helper — no hardcoded list.
    # Threshold 4.0 catches design-YELLOW + runtime-boost RED escalations
    # (legal N6, loan N4), which the previous hardcoded Predicted-only list missed.
    red_nodes = _red_node_specs_from_v02(v02_path, threshold=4.0)
    if not red_nodes:
        raise RuntimeError(
            f"No RED nodes parsed from {v02_path.name}; check Aggregated Final Scores table format."
        )

    # Build diagnosis dict per RED node from ontology cells
    def cells_for(node_id: str) -> dict:
        target = node_id.upper()
        by_axis = {"general_failure": [], "security": [], "handoff": []}
        for c in cells:
            cn = c.get("node", "")
            if not cn:
                continue
            # Match leading token after optional 'loan_' prefix
            stripped = cn[5:] if cn.startswith("loan_") else cn
            first = stripped.split("_", 1)[0].upper()
            if first != target:
                continue
            # sample_source filter: default 'legal' if absent
            cs = c.get("sample_source", "legal")
            if cs != sample_source:
                continue
            axis = c.get("axis", "general_failure")
            by_axis.setdefault(axis, []).append(c)
        return by_axis

    diagnoses = []
    for nid, fn, mode in red_nodes:
        cba = cells_for(nid)
        # Surface evidence.aiid_incidents from ontology cells as pseudo-AIID retrieved set
        pseudo_aiid = []
        for cells_in_axis in cba.values():
            for c in cells_in_axis:
                for inc in (c.get("evidence", {}) or {}).get("aiid_incidents", []) or []:
                    pseudo_aiid.append({
                        "id": inc.get("id"),
                        "title": inc.get("title") or inc.get("relevance", ""),
                        "similarity": 0.75,  # ontology-asserted match → assume strong
                    })
        diagnoses.append({
            "node": {"id": nid, "function": fn, "ai_mode": mode},
            "cells_by_axis": cba,
            "aiid": pseudo_aiid[:5],
        })

    # Run peer review
    rev = SubAgent6PeerReviewer(backend=backend)
    reviews = rev.review_workflow(diagnoses, sample_source, ontology_version)

    # Compose v0.3 output
    title_suffix = "Legal contract review" if sample == "legal" else "Korean loan underwriting"
    header = [
        f"# FDE Agent — Diagnosis Report v0.3 (Sub-Agent 6 Claude Peer Reviewer layer)\n",
        f"**Workflow**: {title_suffix} (Layer 2 sample v0.2)",
        f"**Layer added**: Sub-Agent 6 (Claude Peer Reviewer) — Multi-LLM cross-check on RED nodes",
        f"**Ontology**: {ontology_version} cells = {len(cells)}",
        f"**Backend**: `{reviews[0].backend if reviews else 'mock'}` (BRAIN_PEER env var; "
        f"gemini=VertexGeminiBrain ADC ★ Rapid / gemini_ai_studio=AI Studio (ablation) / "
        f"claude=`claude -p` 구독 우선 / vertex=Vertex AI Model Garden Claude / mock=deterministic)",
        f"**RED nodes reviewed**: {', '.join(r.node_id for r in reviews)}",
        f"**Disagreement flags total**: {sum(len(r.disagreement_flags) for r in reviews)}",
        f"**Alerts**: {sum(1 for r in reviews if r.alert)}/{len(reviews)} nodes\n",
        "---\n",
        "## What's new vs v0.2\n",
        "- v0.2 → all sections preserved verbatim below (Heatmap / Aggregated Final Scores / Handoff Quantification Metrics / RED Node Dossiers)",
        "- v0.3 → **Sub-Agent 6 Claude Peer Reviewer** appended after v0.2 content",
        "- Rubric: 3 axes (alignment / coverage / hallucination_risk) — see `scripts/agents/peer_review_prompt.md`",
        "- Trigger: `peer_confidence < 0.6` OR `disagreement_flags non-empty` → Phoenix `fde.peer.alert = True`",
        "- Phase 1 wire (🅒): Phoenix custom metric emission with span attributes `fde.peer.*`\n",
        "---\n",
        "# v0.2 baseline (preserved)\n",
    ]
    section = render_peer_section(reviews)

    v02_body = v02_content.split("\n", 1)[1] if v02_content.startswith("#") else v02_content
    final = "\n".join(header) + "\n" + v02_body + "\n" + section + "\n"
    out_path = OUTPUT_DIR / f"diagnosis-v0.3-{sample}.md"
    out_path.write_text(final, encoding="utf-8")

    # Console summary
    print(f"\n=== {sample} ({sample_source}) — {len(reviews)} RED nodes reviewed ===")
    for r in reviews:
        emoji = "⚠️" if r.alert else "✅"
        flag_preview = (r.disagreement_flags[0][:80] + " …") if r.disagreement_flags else "—"
        print(f"  [{r.node_id}] backend={r.backend} peer_conf={r.peer_confidence:.2f} {emoji} "
              f"(a={r.axis_scores['alignment']}/c={r.axis_scores['coverage']}/"
              f"h={r.axis_scores['hallucination_risk']}) | {flag_preview}")
    print(f"  → wrote {out_path.relative_to(SCRIPT_DIR)} ({out_path.stat().st_size/1024:.1f} KB)")

    return out_path


if __name__ == "__main__":
    import sys

    # CLI: demo mode
    if len(sys.argv) >= 2 and sys.argv[1] == "demo":
        target = sys.argv[2] if len(sys.argv) >= 3 else "all"
        backend_arg = sys.argv[3] if len(sys.argv) >= 4 else os.environ.get("BRAIN_PEER", "mock")
        targets = ["legal", "loan"] if target == "all" else [target]
        for t in targets:
            _e2e_demo(t, backend=backend_arg)
        sys.exit(0)

    # Smoke test — mock backend, 3 calibration anchors
    rev = SubAgent6PeerReviewer(backend="mock")

    # Anchor 1: loan_N7 silent escalation (peer concur expected)
    loan_n7 = {
        "node": {"id": "N7", "function": "자동 결정 엔진", "ai_mode": "Full automation"},
        "cells_by_axis": {
            "general_failure": [{"cell_id": "loan_N7_general_failure", "primary_failure_mode": "false_positive_approval", "risk_score": 4.8}],
            "security": [{"cell_id": "loan_N7_security", "primary_threats": [{"id": "LLM06", "title": "Excessive Agency"}, {"id": "LLM02"}], "risk_score": 4.8}],
            "handoff": [{"cell_id": "loan_N7_handoff", "primary_handoff_risk": "bias_cascade_from_ACS", "risk_score": 4.7, "heuristic_source": "본인 IP — K-PIPA Art 22-2"}],
        },
        "aiid": [
            {"id": "incident_911", "title": "Apple Card gender bias loan limits", "similarity": 0.88},
            {"id": "incident_704", "title": "Algorithmic redlining mortgage", "similarity": 0.81},
            {"id": "incident_602", "title": "Bias cascade consumer credit", "similarity": 0.75},
        ],
    }
    r1 = rev.review_node(loan_n7, "korean_loan")
    print(f"[loan_N7] peer_conf={r1.peer_confidence} alert={r1.alert} flags={r1.disagreement_flags} | {r1.alternative_view[:80]}")
    assert r1.peer_confidence >= 0.7, f"expected concur, got {r1.peer_confidence}"

    # Anchor 2: legal N3 borderline (peer flags thin security + weak AIID)
    legal_n3 = {
        "node": {"id": "N3", "function": "LLM risk flagging", "ai_mode": "Full automation (LLM)"},
        "cells_by_axis": {
            "general_failure": [{"cell_id": "N3_general_failure", "primary_failure_mode": "false_negative", "risk_score": 4.2}],
            "security": [{"cell_id": "N3_security", "primary_threats": [{"id": "LLM09"}], "risk_score": 3.5}],
            "handoff": [{"cell_id": "N3_handoff", "primary_handoff_risk": "weighting_loss", "risk_score": 4.0, "heuristic_source": "본인 IP"}],
        },
        "aiid": [
            {"id": "incident_320", "title": "Image misclassification", "similarity": 0.51},
            {"id": "incident_222", "title": "Generic content moderation", "similarity": 0.42},
            {"id": "incident_188", "title": "Search ranking bias", "similarity": 0.39},
            {"id": "incident_101", "title": "Recommendation system drift", "similarity": 0.33},
            {"id": "incident_055", "title": "Speech recognition error", "similarity": 0.31},
        ],
    }
    r2 = rev.review_node(legal_n3, "legal")
    print(f"[legal_N3] peer_conf={r2.peer_confidence} alert={r2.alert} flags={r2.disagreement_flags}")
    assert r2.alert, f"expected disagreement, got alert={r2.alert} conf={r2.peer_confidence}"
    assert len(r2.disagreement_flags) >= 1

    # Anchor 3: loan_N9 KYC with missing handoff axis
    loan_n9 = {
        "node": {"id": "N9", "function": "외부 KYC 전송", "ai_mode": "Full automation"},
        "cells_by_axis": {
            "general_failure": [{"cell_id": "loan_N9_general_failure", "primary_failure_mode": "kyc_false_negative", "risk_score": 4.5}],
            "security": [{"cell_id": "loan_N9_security", "primary_threats": [{"id": "LLM02"}, {"id": "LLM06"}], "risk_score": 4.3}],
            "handoff": [],   # missing!
        },
        "aiid": [{"id": "incident_777", "title": "deepfake ID document verification", "similarity": 0.78}],
    }
    r3 = rev.review_node(loan_n9, "korean_loan")
    print(f"[loan_N9] peer_conf={r3.peer_confidence} alert={r3.alert} flags={r3.disagreement_flags}")
    assert r3.alert
    assert any("Handoff axis missing" in f for f in r3.disagreement_flags), \
        f"expected handoff-missing flag, got: {r3.disagreement_flags}"

    # Render section
    section = render_peer_section([r1, r2, r3])
    print("\n--- render_peer_section (first 600 chars) ---")
    print(section[:600])

    # Phoenix attrs sanity
    attrs = r1.phoenix_attributes()
    assert "fde.peer.confidence" in attrs
    assert "fde.peer.alert" in attrs

    print("\nsub_agent_6_peer_review.py smoke test passed.")
