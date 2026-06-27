"""
FDE Agent — Brain Factory (Vertex Gemini ↔ AI Studio Gemini ↔ Claude ↔ Mock)

env var BRAIN:
  - "gemini"             → VertexGeminiBrain  (★ Rapid Agent default — Vertex AI
                                                + ADC, google-genai SDK with
                                                GOOGLE_GENAI_USE_VERTEXAI=True).
                            "Google Cloud AI tools" 규정 정합. AI Studio key 불필요.
  - "gemini_ai_studio"   → GeminiBrain         (legacy AI Studio path; ablation /
                                                non-Rapid 컨텍스트. Keychain key 사용)
  - "claude"             → ClaudeBrain         (claude -p subprocess, Max 구독 우선)
  - "mock"               → MockBrain           (deterministic stub, regression test)
  - 미지정                → 자동 polling 순서:
                            VertexGemini (ADC ready) → GeminiBrain (Keychain key) →
                            ClaudeBrain (CLI) → MockBrain

Auth matrix:
  - VertexGeminiBrain: ADC (Application Default Credentials).
        local: `gcloud auth application-default login`
        Cloud Run: 자동 (runtime service account).
        env: GOOGLE_CLOUD_PROJECT + GOOGLE_CLOUD_LOCATION; SDK는
             GOOGLE_GENAI_USE_VERTEXAI=True 일 때 Vertex로 라우팅 (이 모듈이 자동 설정).
        No API key (Cloud Run 환경에서 secret/keychain 의존성 0).
  - GeminiBrain: AI Studio key.
        1) env GEMINI_API_KEY
        2) macOS Keychain `security find-generic-password -s gemini_api -a key -w`
        3) 없으면 not-ready → caller graceful degrade.

Verified SDK paths (2026-05 Google Cloud official quickstart):
  - Vertex AI: `pip install --upgrade google-genai` (NOT google-cloud-aiplatform).
               `from google import genai` + `genai.Client(...)` with vertexai routing.
  - AI Studio: `pip install google-generativeai` (legacy GeminiBrain — 유지).

본인 secret 입력 정책 정합 [feedback_secret-input-claude-code]:
  Claude Code prompt에 key 직접 입력 X — Keychain만이 안전. env var fallback은
  shell session 내 1회 사용 (.env 박지 말 것). VertexGeminiBrain은 secret 미사용
  (ADC만) — Cloud Run 운영 정합.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Any, Optional


KEYCHAIN_SERVICE = "gemini_api"
KEYCHAIN_ACCOUNT = "key"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
DEFAULT_CLAUDE_TIMEOUT = 120

# --- Deployment-runtime brains (UiPath path only — multi-model allowed) ------
# Per Rapid rules, the Rapid submission path is Gemini-only; these API-backed
# brains replace the `claude -p` subprocess for deployed runtimes (Cloud Run /
# UiPath Coded Agent) where a CLI is unavailable. They are reachable ONLY when
# policy != "rapid" (enforced in get_brain). See architecture.md § Model Policy.
OPENAI_KEYCHAIN_SERVICE = "openai_api"
DEFAULT_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
# Vertex Model Garden Claude — model id verified at deploy time against
# https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude .
# Operator overrides via VERTEX_CLAUDE_MODEL. Vertex routes by endpoint, so the
# id carries an `@version` suffix (publisher model resource style).
DEFAULT_VERTEX_CLAUDE_MODEL = os.environ.get("VERTEX_CLAUDE_MODEL", "claude-sonnet-4-5@20250929")
DEFAULT_VERTEX_CLAUDE_MAX_TOKENS = int(os.environ.get("VERTEX_CLAUDE_MAX_TOKENS", "2048"))

# Vertex AI Gemini — verified 2026-05 Google Cloud official quickstart.
# Operators MUST verify the exact stable model snapshot at deploy time
# (https://cloud.google.com/vertex-ai/generative-ai/docs/models) and override
# via VERTEX_GEMINI_MODEL env. Default is the quickstart-documented value.
DEFAULT_VERTEX_GEMINI_MODEL = os.environ.get("VERTEX_GEMINI_MODEL", "gemini-2.5-flash")


# --- Rapid Gemini-only guard (Lane 🅑 — see Factory scope note) ----------------
# Opt-in (default '0') — 공유 코드베이스라 UiPath/dev multi-model 을 깨지 않도록
# 비활성 기본. Rapid Cloud Run deploy 가 FDE_RAPID=1 을 명시 주입할 때만
# 비-Gemini backend(claude / vertex_claude / openai / gemini_ai_studio)를 차단한다.
_NON_GEMINI_SELECTORS = (
    "claude",
    "vertex_claude", "vertex-claude",
    "openai",
    "gemini_ai_studio", "ai_studio", "ai-studio",
)


def _rapid_pinned() -> bool:
    """True iff FDE_RAPID env ∈ {1,true,yes}. 미설정 시 False (비-Rapid 기본)."""
    return os.environ.get("FDE_RAPID", "0").lower() in ("1", "true", "yes")


# =============================================================
# Brain ABC
# =============================================================

class Brain(ABC):
    """Common interface for all sub-agent brain implementations."""
    name: str = "abstract"
    model_id: str = "n/a"

    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> str:
        """텍스트 → 텍스트 generation. multimodal은 generate_multimodal()."""
        ...

    def generate_multimodal(self, prompt: str, image_path: Optional[str] = None, **kwargs) -> str:
        """이미지 첨부 가능 — default는 text-only fallback."""
        return self.generate(prompt, **kwargs)

    def healthcheck(self) -> dict:
        """name/model + readiness status."""
        return {"name": self.name, "model": self.model_id, "ready": True}


# =============================================================
# VertexGeminiBrain (★ Rapid Agent default — Vertex AI Gemini via ADC)
# =============================================================

class VertexGeminiBrain(Brain):
    """Gemini on Vertex AI via google-genai SDK + Application Default Credentials.

    Per Google Cloud Vertex AI official quickstart (verified 2026-05): the unified
    `google-genai` SDK is the current documented path. `GOOGLE_GENAI_USE_VERTEXAI`
    selects Vertex routing instead of AI Studio; ADC supplies credentials with no
    API key. On Cloud Run the runtime service account satisfies ADC automatically;
    locally `gcloud auth application-default login` does the same.

    Brain ABC compat (C lane `scripts/serve/app.py` 계약):
      - zero-arg constructor works
      - `healthcheck()` returns dict with bool `"ready"` (no API call cost)
      - `generate(prompt)` returns str (raises on misconfig — caller wraps)

    No secret loading. No Keychain. No `.env`. Cloud Run-native.
    """
    name = "vertex-gemini"

    def __init__(self,
                 model: Optional[str] = None,
                 project: Optional[str] = None,
                 location: Optional[str] = None):
        self.model_id = model or DEFAULT_VERTEX_GEMINI_MODEL
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION")
        # Idempotent — route google-genai SDK to Vertex (does not overwrite if
        # operator pre-set the env). Quickstart-documented control variable.
        os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
        self._sdk_available = False
        self._genai_module = None
        self._http_options_cls = None
        self._client = None
        try:
            from google import genai  # type: ignore
            from google.genai.types import HttpOptions  # type: ignore
            self._genai_module = genai
            self._http_options_cls = HttpOptions
            self._sdk_available = True
        except ImportError:
            # graceful degradation — caller checks healthcheck()['ready']
            self._sdk_available = False

    def _ensure_client(self):
        """Lazy client construction. Returns the client or None if init fails.
        Does not raise — `generate()` is the appropriate place to raise."""
        if self._client is not None or not self._sdk_available:
            return self._client
        try:
            self._client = self._genai_module.Client(
                http_options=self._http_options_cls(api_version="v1"),
            )
        except Exception:
            self._client = None
        return self._client

    def healthcheck(self) -> dict:
        """Readiness heuristic — does not perform a real auth round-trip.
        Ready iff SDK installed AND (project env set OR explicit ADC file env set).
        Cloud Run satisfies the project condition via metadata server when
        GOOGLE_CLOUD_PROJECT is propagated; operators should set both project and
        location envs explicitly per quickstart."""
        adc_visible = bool(self.project) or bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        return {
            "name": self.name,
            "model": self.model_id,
            "ready": bool(self._sdk_available and adc_visible),
            "sdk_installed": self._sdk_available,
            "project_env_set": bool(self.project),
            "location_env_set": bool(self.location),
            "vertex_routing": os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False"),
        }

    def generate(self, prompt: str, **kwargs) -> str:
        if not self._sdk_available:
            raise RuntimeError(
                "VertexGeminiBrain not ready: google-genai SDK not installed "
                "(`pip install --upgrade google-genai`)"
            )
        client = self._ensure_client()
        if client is None:
            raise RuntimeError(
                "VertexGeminiBrain client init failed — verify ADC "
                "(`gcloud auth application-default login`) and "
                "GOOGLE_CLOUD_PROJECT/LOCATION env vars."
            )
        response = client.models.generate_content(
            model=self.model_id,
            contents=prompt,
            **kwargs,
        )
        text = getattr(response, "text", None)
        return text if text is not None else str(response)


# =============================================================
# GeminiBrain (Google AI Studio API)  ← legacy / ablation path
# =============================================================

class GeminiBrain(Brain):
    name = "gemini"

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL, api_key: Optional[str] = None):
        self.model_id = model
        self._api_key = api_key or self._load_key()
        self._sdk_available = False
        self._model = None
        if self._api_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self._api_key)
                self._model = genai.GenerativeModel(model)
                self._sdk_available = True
            except ImportError:
                # SDK 미설치 — graceful degradation
                self._sdk_available = False

    @staticmethod
    def _load_key() -> Optional[str]:
        key = os.environ.get("GEMINI_API_KEY")
        if key:
            return key.strip()
        try:
            r = subprocess.run(
                ["security", "find-generic-password",
                 "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if r.returncode == 0:
                return r.stdout.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def ready(self) -> bool:
        return bool(self._api_key) and self._sdk_available

    def healthcheck(self) -> dict:
        return {
            "name": self.name, "model": self.model_id,
            "ready": self.ready(),
            "api_key_present": bool(self._api_key),
            "sdk_installed": self._sdk_available,
        }

    def generate(self, prompt: str, **kwargs) -> str:
        if not self.ready():
            raise RuntimeError(
                "GeminiBrain not ready: "
                f"api_key={'set' if self._api_key else 'MISSING'}, "
                f"sdk={'installed' if self._sdk_available else 'MISSING'}"
            )
        response = self._model.generate_content(prompt, **kwargs)
        return getattr(response, "text", str(response))

    def generate_multimodal(self, prompt: str, image_path: Optional[str] = None, **kwargs) -> str:
        if not self.ready():
            raise RuntimeError("GeminiBrain not ready (multimodal)")
        if image_path is None:
            return self.generate(prompt, **kwargs)
        import google.generativeai as genai
        # SDK upload 후 generate
        uploaded = genai.upload_file(path=image_path)
        response = self._model.generate_content([prompt, uploaded], **kwargs)
        return getattr(response, "text", str(response))


# =============================================================
# ClaudeBrain (claude -p subprocess, Max 구독)
# =============================================================

class ClaudeBrain(Brain):
    name = "claude"
    model_id = "claude-opus-4-7[1m]"  # Max 구독 default — 본 sprint는 개발 편의

    def __init__(self, timeout: int = DEFAULT_CLAUDE_TIMEOUT):
        self.timeout = timeout
        self._cli_available = shutil.which("claude") is not None

    def ready(self) -> bool:
        return self._cli_available

    def healthcheck(self) -> dict:
        return {"name": self.name, "model": self.model_id,
                "ready": self.ready(), "cli_installed": self._cli_available}

    def generate(self, prompt: str, **kwargs) -> str:
        if not self.ready():
            raise RuntimeError("ClaudeBrain not ready: `claude` CLI not on PATH")
        r = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=self.timeout, check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"claude -p exit {r.returncode}: {r.stderr[:200]}")
        return r.stdout.strip()


# =============================================================
# VertexClaudeBrain (★ UiPath deploy — Vertex Model Garden Claude via AnthropicVertex)
# =============================================================

class VertexClaudeBrain(Brain):
    """Claude on Vertex AI Model Garden via the `anthropic[vertex]` SDK + ADC.

    ★ UiPath path ONLY (multi-model allowed). MUST NOT be reachable on the Rapid
    submission path (Gemini-only) — enforced by the policy gate in get_brain().

    Replaces ClaudeBrain (`claude -p` subprocess) for *deployed* runtimes where a
    CLI is unavailable. Auth is ADC (no API key), identical to VertexGeminiBrain:
        local      : `gcloud auth application-default login`
        Cloud Run  : runtime service account (automatic)
    Per Anthropic + Google Cloud docs: `pip install -U 'anthropic[vertex]'`, then
    `from anthropic import AnthropicVertex`; the model is selected by the Vertex
    endpoint, so the model id carries an `@version` suffix.
    """
    name = "vertex-claude"

    def __init__(self,
                 model: Optional[str] = None,
                 project: Optional[str] = None,
                 region: Optional[str] = None,
                 max_tokens: int = DEFAULT_VERTEX_CLAUDE_MAX_TOKENS):
        self.model_id = model or DEFAULT_VERTEX_CLAUDE_MODEL
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
        # Vertex Claude is region-pinned (multi-region "us"/"eu" or e.g. us-east5).
        self.region = region or os.environ.get("CLOUD_ML_REGION") or os.environ.get("GOOGLE_CLOUD_LOCATION")
        self.max_tokens = max_tokens
        self._sdk_available = False
        self._client_cls = None
        self._client = None
        try:
            from anthropic import AnthropicVertex  # type: ignore
            self._client_cls = AnthropicVertex
            self._sdk_available = True
        except ImportError:
            self._sdk_available = False

    def _ensure_client(self):
        if self._client is not None or not self._sdk_available:
            return self._client
        try:
            kwargs = {}
            if self.project:
                kwargs["project_id"] = self.project
            if self.region:
                kwargs["region"] = self.region
            self._client = self._client_cls(**kwargs)
        except Exception:
            self._client = None
        return self._client

    def healthcheck(self) -> dict:
        adc_visible = bool(self.project) or bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
        return {
            "name": self.name,
            "model": self.model_id,
            "ready": bool(self._sdk_available and adc_visible),
            "sdk_installed": self._sdk_available,
            "project_set": bool(self.project),
            "region_set": bool(self.region),
        }

    def generate(self, prompt: str, **kwargs) -> str:
        if not self._sdk_available:
            raise RuntimeError(
                "VertexClaudeBrain not ready: anthropic[vertex] SDK not installed "
                "(`pip install -U 'anthropic[vertex]'`)"
            )
        client = self._ensure_client()
        if client is None:
            raise RuntimeError(
                "VertexClaudeBrain client init failed — verify ADC "
                "(`gcloud auth application-default login`) and GOOGLE_CLOUD_PROJECT / region."
            )
        max_tokens = kwargs.pop("max_tokens", self.max_tokens)
        resp = client.messages.create(
            model=self.model_id,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        # Concatenate text blocks (Messages API content is a list of blocks).
        parts = [getattr(b, "text", "") for b in getattr(resp, "content", []) or []]
        text = "".join(parts).strip()
        return text or str(resp)


# =============================================================
# OpenAIBrain (★ UiPath deploy — OpenAI API, $200 credit)
# =============================================================

class OpenAIBrain(Brain):
    """OpenAI Chat Completions brain.

    ★ UiPath path ONLY (multi-model allowed). MUST NOT be reachable on the Rapid
    submission path — enforced by the policy gate in get_brain().

    Key resolution (secret-safe, no `.env`):
        1) env OPENAI_API_KEY
        2) macOS Keychain `security find-generic-password -s openai_api -a key -w`
    """
    name = "openai"

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL, api_key: Optional[str] = None):
        self.model_id = model
        self._api_key = api_key or self._load_key()
        self._sdk_available = False
        self._client = None
        if self._api_key:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self._api_key)
                self._sdk_available = True
            except ImportError:
                self._sdk_available = False

    @staticmethod
    def _load_key() -> Optional[str]:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            return key.strip()
        try:
            r = subprocess.run(
                ["security", "find-generic-password",
                 "-s", OPENAI_KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if r.returncode == 0:
                return r.stdout.strip() or None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def ready(self) -> bool:
        return bool(self._api_key) and self._sdk_available

    def healthcheck(self) -> dict:
        return {
            "name": self.name, "model": self.model_id,
            "ready": self.ready(),
            "api_key_present": bool(self._api_key),
            "sdk_installed": self._sdk_available,
        }

    def generate(self, prompt: str, **kwargs) -> str:
        if not self.ready():
            raise RuntimeError(
                "OpenAIBrain not ready: "
                f"api_key={'set' if self._api_key else 'MISSING'}, "
                f"sdk={'installed' if self._sdk_available else 'MISSING (pip install openai)'}"
            )
        resp = self._client.chat.completions.create(
            model=self.model_id,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return (resp.choices[0].message.content or "").strip()


# =============================================================
# MockBrain (deterministic stub — regression / SDK 미발급 fallback)
# =============================================================

class MockBrain(Brain):
    """
    Deterministic stub. Prompt를 hash해서 짧은 응답 박음.
    Sub-agent wrappers는 MockBrain일 경우 LLM 호출 결과 대신 ontology-only path로 fallback해야 함
    (wrapper 내부 책임).
    """
    name = "mock"
    model_id = "mock-deterministic-v0.1"

    def ready(self) -> bool:
        return True

    def generate(self, prompt: str, **kwargs) -> str:
        # 단순 echo + length stamp — caller가 mock인지 식별 가능
        return f"[MOCK BRAIN] prompt_hash={hash(prompt) & 0xFFFFFF:06x} len={len(prompt)}"


# =============================================================
# Factory
# =============================================================
#
# ★ Scope note (Sprint 7): this factory ADDS the deploy-runtime multi-model
# brains (VertexClaudeBrain / OpenAIBrain) as selectable options. The Rapid
# Gemini-only enforcement (the `FDE_RAPID` guard) is owned by Lane 🅑 and is
# intentionally NOT implemented here — do not add a policy gate in this file.
# Non-Gemini brains must only be SELECTED on the UiPath path; the guard that
# blocks them on the Rapid path lives wherever Lane 🅑 places it.

def get_brain(name: Optional[str] = None) -> Brain:
    """
    env var BRAIN 또는 explicit name → Brain instance.

    Selector semantics (post 2026-05-29 Model Policy closure):
      "gemini" / "vertex_gemini" / "vertex-gemini"
            → VertexGeminiBrain  (Rapid default, ADC, no key)
      "gemini_ai_studio" / "ai_studio" / "ai-studio"
            → GeminiBrain        (legacy AI Studio, Keychain key)
      "claude"                          → ClaudeBrain        (local dev, claude -p subprocess)
      "vertex_claude" / "vertex-claude" → VertexClaudeBrain  (deploy runtime, AnthropicVertex + ADC)
            ★ UiPath path only — not for Rapid (Lane 🅑 FDE_RAPID guard enforces).
      "openai"                          → OpenAIBrain        (deploy runtime, $200 credit)
            ★ UiPath path only — not for Rapid (Lane 🅑 FDE_RAPID guard enforces).
      "mock"                            → MockBrain
      "" / "auto" → auto-detect:
            VertexGeminiBrain (ready) → GeminiBrain (ready) → ClaudeBrain (ready) → MockBrain
    """
    requested = (name or os.environ.get("BRAIN") or "").lower().strip()
    # Rapid pin (FDE_RAPID=1): 명시적 비-Gemini backend 요청 전수 차단.
    # claude / vertex_claude / openai / gemini_ai_studio → RuntimeError.
    if _rapid_pinned() and requested in _NON_GEMINI_SELECTORS:
        raise RuntimeError(
            f"BRAIN={requested!r} forbidden under FDE_RAPID=1 — non-Gemini AI tools "
            "not permitted on the Rapid submission path "
            "(use gemini / vertex_gemini, or mock)."
        )
    if requested in ("gemini", "vertex_gemini", "vertex-gemini"):
        return VertexGeminiBrain()
    if requested in ("gemini_ai_studio", "ai_studio", "ai-studio"):
        return GeminiBrain()
    if requested == "claude":
        return ClaudeBrain()
    if requested in ("vertex_claude", "vertex-claude"):
        return VertexClaudeBrain()
    if requested == "openai":
        return OpenAIBrain()
    if requested == "mock":
        return MockBrain()
    if requested and requested not in ("", "auto"):
        raise ValueError(
            f"Unknown brain: {requested!r} "
            "(expected gemini/gemini_ai_studio/claude/vertex_claude/openai/mock/auto)"
        )
    # auto-detect — Vertex first (Rapid Cloud Run default), AI Studio fallback
    # (dev w/ Keychain key), Claude (Max subscription), Mock (last resort).
    v = VertexGeminiBrain()
    if v.healthcheck().get("ready"):
        return v
    if _rapid_pinned():
        # Rapid: Vertex Gemini 미준비 시 비-Gemini fallback(AI Studio/Claude/OpenAI/
        # VertexClaude) 금지 → MockBrain 직행 (규정 정합, secret 의존성 0).
        return MockBrain()
    g = GeminiBrain()
    if g.ready():
        return g
    c = ClaudeBrain()
    if c.ready():
        return c
    return MockBrain()


if __name__ == "__main__":
    # diagnostic dump — secret 자체는 출력 X (정책 정합)
    print("=== brain auto-detect ===")
    b = get_brain()
    hc = b.healthcheck()
    print(f"  selected: {hc}")
    print()
    print("=== all brains healthcheck ===")
    for ctor in (VertexGeminiBrain, GeminiBrain, ClaudeBrain,
                 VertexClaudeBrain, OpenAIBrain, MockBrain):
        try:
            print(f"  {ctor.__name__}: {ctor().healthcheck()}")
        except Exception as e:
            print(f"  {ctor.__name__}: ERROR {type(e).__name__}: {e}")
