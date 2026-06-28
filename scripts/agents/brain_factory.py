"""FDE Agent — Brain Factory (model-agnostic, GCP-free).

The deterministic ontology + RAG diagnosis core needs NO LLM. A "brain" is only
used for the optional executive-summary narrative, applied at the orchestration
layer and resolved from the `BRAIN` env var (default: mock). On the UiPath track
brains are selectable (Claude / OpenAI); none is invoked in the shipped demo,
which runs the deterministic core.

    BRAIN=claude   -> ClaudeBrain  (local `claude -p` subprocess; dev only)
    BRAIN=openai   -> OpenAIBrain  (OpenAI SDK; key via env / Orchestrator credential)
    BRAIN=mock     -> MockBrain    (deterministic, no network)

Selection never raises: an unavailable backend falls back to MockBrain, so the
diagnosis core path is unaffected whether or not a brain is present.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from typing import Optional


class Brain(ABC):
    """Common interface for all brain implementations."""
    name: str = "abstract"
    model_id: str = "n/a"

    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> str:
        ...

    def generate_multimodal(self, prompt: str, image_path: Optional[str] = None, **kwargs) -> str:
        """Default is text-only fallback (no image support in this build)."""
        return self.generate(prompt, **kwargs)

    def healthcheck(self) -> dict:
        return {"name": self.name, "model": self.model_id, "ready": True}


class MockBrain(Brain):
    """Deterministic stub for unit tests and offline / degraded runs."""
    name = "mock"
    model_id = "deterministic-stub"

    def generate(self, prompt: str, **kwargs) -> str:
        return "[mock brain] " + prompt[:120]


class ClaudeBrain(Brain):
    """Local Claude via the `claude -p` subprocess (Max subscription; dev only).

    No API key and no cloud SDK. healthcheck() reports not-ready when the CLI is
    absent, so callers fall back to the mock path.
    """
    name = "claude"
    model_id = "claude-cli"

    def __init__(self, timeout: int = 120):
        self.timeout = timeout
        self._cli = shutil.which("claude")

    def healthcheck(self) -> dict:
        return {"name": self.name, "model": self.model_id, "ready": bool(self._cli)}

    def generate(self, prompt: str, **kwargs) -> str:
        if not self._cli:
            raise RuntimeError("claude CLI not found on PATH")
        proc = subprocess.run(
            [self._cli, "-p", prompt], capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed: {proc.stderr.strip()[:200]}")
        return proc.stdout.strip()


class OpenAIBrain(Brain):
    """OpenAI SDK brain. Key from `OPENAI_API_KEY` (or an Orchestrator credential
    injected as that env var). Lazy import keeps the SDK optional."""
    name = "openai"
    model_id = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def healthcheck(self) -> dict:
        return {"name": self.name, "model": self.model_id, "ready": bool(os.environ.get("OPENAI_API_KEY"))}

    def generate(self, prompt: str, **kwargs) -> str:
        from openai import OpenAI  # lazy import; optional dependency

        client = OpenAI()
        resp = client.chat.completions.create(
            model=self.model_id, messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content or ""


_BRAINS = {"mock": MockBrain, "claude": ClaudeBrain, "openai": OpenAIBrain}


def get_brain(requested: Optional[str] = None) -> Brain:
    """Resolve a Brain from `requested` (or the `BRAIN` env var; default mock).

    Unknown or unavailable selections fall back to MockBrain. Never raises.
    """
    name = (requested or os.environ.get("BRAIN") or "mock").strip().lower()
    ctor = _BRAINS.get(name, MockBrain)
    try:
        return ctor()
    except Exception:
        return MockBrain()
