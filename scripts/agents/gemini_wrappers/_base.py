"""
FDE Agent — Gemini wrappers common base.

각 sub-agent wrapper는:
  1. Brain instance 주입받음 (Gemini / Claude / Mock — brain_factory가 결정)
  2. ontology / standards YAML 로딩
  3. run(input) → output dict 단일 entry
  4. Brain이 not-ready (MockBrain) 일 경우 ontology-only deterministic path로 graceful degradation

이 base class는 공통 인프라만:
  - DATA 경로 resolution
  - YAML lazy load
  - brain.ready() 분기 helper
  - LLM 호출 retry / 빈 응답 fallback
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from ..brain_factory import Brain, MockBrain, get_brain


DATA_DIR = Path(__file__).parent.parent.parent / "data"


_yaml_cache: dict[str, Any] = {}


def load_yaml(filename: str) -> dict:
    """캐시된 YAML load. DATA_DIR 기준."""
    if filename in _yaml_cache:
        return _yaml_cache[filename]
    path = DATA_DIR / filename
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _yaml_cache[filename] = data
    return data


class GeminiSubAgentBase:
    """All gemini_wrappers/ sub-agents inherit this. brain은 외부에서 주입."""

    name: str = "sub_agent_base"

    def __init__(self, brain: Optional[Brain] = None):
        self.brain = brain or get_brain()

    @property
    def is_mock(self) -> bool:
        """MockBrain 또는 not-ready brain — wrapper는 ontology-only path 강제."""
        return isinstance(self.brain, MockBrain) or not getattr(self.brain, "ready", lambda: True)()

    def llm(self, prompt: str, fallback: str = "", **kwargs) -> str:
        """
        Brain.generate() 시도. not-ready / 빈 응답 / 예외 시 fallback string return.
        wrapper 호출자는 LLM 응답 + fallback 둘 다 가정한 후속 parse 로직 유지.
        """
        if self.is_mock:
            return fallback
        try:
            out = self.brain.generate(prompt, **kwargs)
            return out.strip() if out else fallback
        except Exception:
            # brain failure는 silent fallback — pipeline 정지보다 ontology-only 보장 우선
            return fallback

    def info(self) -> dict:
        return {"name": self.name, "brain": self.brain.healthcheck()}
