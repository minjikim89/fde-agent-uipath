"""FDE Agent — Sub-agents (Sub-Agent 5 Mitigation Recommender + Sub-Agent 6 Claude Peer Reviewer).

Sub-agent re-exports are optional: a venue export build may strip a sub-agent
(e.g. the Gemini-only Rapid build excludes Sub-Agent 6). Guard each import so that
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

try:
    from .sub_agent_6_peer_review import (
        PeerReviewResult,
        SubAgent6PeerReviewer,
        render_peer_section,
    )
    __all__ += ["PeerReviewResult", "SubAgent6PeerReviewer", "render_peer_section"]
except ImportError:
    pass
