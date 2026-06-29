"""FDE Agent — Sub-agents (Sub-Agent 5 Mitigation Recommender).

Sub-agent re-exports are optional: a venue export build may strip a sub-agent
(e.g. a lean build may omit an optional sub-agent). Guard each import so that
importing this package (and siblings like agents.brain_factory) never fails when a
sub-agent module is intentionally absent.
"""
__all__ = []

try:
    from .sub_agent_5_mitigation import (
        MitigationOption,
        NodeMitigationDossier,
        SubAgent5MitigationRecommender,
    )
    __all__ += ["MitigationOption", "NodeMitigationDossier", "SubAgent5MitigationRecommender"]
except ImportError:
    pass

