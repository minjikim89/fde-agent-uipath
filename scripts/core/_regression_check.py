"""
Core engine regression check — assert DiagnosisEngine reproduces the
diagnose.py v0.2 aggregated final scores for the bundled samples.

Run:  scripts/.venv/bin/python -m core._regression_check
(requires chromadb + sentence-transformers + torch in the active venv)
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from core import DiagnosisEngine, WorkflowInput, DiagnosisOptions  # noqa: E402


def run_engine(sample: str) -> dict:
    engine = DiagnosisEngine()
    res = engine.diagnose(WorkflowInput(sample_name=sample), DiagnosisOptions())
    return res.to_dict()


def main() -> int:
    ok = True
    for sample in ("legal", "loan"):
        print(f"\n=== engine.diagnose({sample!r}) ===")
        res = run_engine(sample)
        print(f"  status={res['status']} degraded={res['degraded']} "
              f"n_nodes={res['graph']['n_nodes']} n_red={res['graph']['n_red']}")
        print(f"  notes={res['notes']}")
        print(f"  max_final={res['max_final_score']} runtime_alerts={res['runtime_alerts']} "
              f"hitl={res['hitl_required']}")
        print(f"  hitl_reason={res['hitl_reason'][:120]}")
        print("  aggregated final scores:")
        for d in res["diagnoses"]:
            print(f"    {d['node_id']:5s} final={d['final_score']:.2f} {d['color']:7s} "
                  f"alerts={d['runtime_metric_alerts'][:2]}")
        if res["status"] != "ok":
            ok = False
    print("\n" + ("REGRESSION CHECK OK" if ok else "REGRESSION CHECK FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
