"""
FDE Agent — Shared Diagnosis Core
=================================

The brain-agnostic diagnosis engine (ontology + RAG + 3-axis scoring) shared by
both hackathon submission paths:

    ADK (Rapid path)          ─┐
                               ├─→  from core import DiagnosisEngine, tools
    CrewAI (UiPath, multi-model)─┘

Public API:
    DiagnosisEngine            — resource owner + pipeline (engine.py)
    WorkflowInput / DiagnosisOptions / DiagnosisResult / HitlThresholds (contracts.py)
    tools.*                    — pure, dependency-injected tool functions (tools.py)

Import contract: `import core` is light. Heavy deps (chromadb / sentence-
transformers / torch) load only inside DiagnosisEngine._ensure_rag().
"""
from __future__ import annotations

import sys
from pathlib import Path

# Put scripts/ on sys.path so core.tools can use the existing absolute-import
# convention (`from metrics.ips import ...`, `from agents.aggregator import ...`)
# regardless of how core is imported (CLI, FastAPI, UiPath pack, CrewAI venv).
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from .contracts import (  # noqa: E402
    WorkflowInput,
    DiagnosisOptions,
    DiagnosisResult,
    HitlThresholds,
    GraphSummary,
    VALID_WORKFLOW_FORMATS,
    VALID_SAMPLE_SOURCES,
)
from .engine import DiagnosisEngine, SAMPLE_FILES, SAMPLE_SOURCE, SAMPLE_TITLES  # noqa: E402
from . import tools  # noqa: E402

__all__ = [
    "DiagnosisEngine",
    "WorkflowInput",
    "DiagnosisOptions",
    "DiagnosisResult",
    "HitlThresholds",
    "GraphSummary",
    "VALID_WORKFLOW_FORMATS",
    "VALID_SAMPLE_SOURCES",
    "SAMPLE_FILES",
    "SAMPLE_SOURCE",
    "SAMPLE_TITLES",
    "tools",
]
