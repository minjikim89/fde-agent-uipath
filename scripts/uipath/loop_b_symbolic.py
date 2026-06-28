"""
Loop B-Symbolic — Mitigation Application to BPMN Workflow
==========================================================

Phase 2 UiPath AgentHack Track 2 artifact.
Hero Moment scene Beat (e) Resolution core module:
    diagnosis → mitigation option (Sub-Agent 5) → BPMN XML patch → Maestro re-deploy

Symbolic option (vs Recommendation-only / vs Hybrid w/ approval gate):
    See `_research/2026-05-26-loop-b-symbolic-decision.md` for rationale.

Dry-run is possible regardless of UiPath SDK access:
    Maestro re-deploy trigger is stubbed (wire after Labs access is received).
    Pure XML patching layer is standalone — `xml.etree.ElementTree` only.

Usage (CLI sanity):
    # apply must_fix option to N7 (loan workflow) — produces .applied.xml
    python loop_b_symbolic.py \\
        --bpmn ../data/sample-workflows/loan-uw-v0.1.bpmn \\
        --node N7 \\
        --option must_fix:fair_lending_audit \\
        --label "Fair Lending audit gate + ACS feature attribution"

    # sanity against the FDE Agent BPMN itself
    python loop_b_symbolic.py --self-test

Python API:
    from loop_b_symbolic import apply_mitigation

    updated_path = apply_mitigation(
        bpmn_path=Path("loan-uw-v0.1.bpmn"),
        node_id="N7",
        option_id="must_fix:fair_lending_audit",
        option_label="Fair Lending audit gate + ACS feature attribution",
    )
    # Returns Path to updated XML. UiPath Maestro re-deploy is stubbed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

# --------------------------------------------------------------------------
# UiPath SDK stub — import path to be determined after access is received (uipath / uipath_sdk /
# uipath.maestro / uipath_agents — one of these).
# --------------------------------------------------------------------------
try:
    import uipath  # type: ignore  # noqa: F401
    UIPATH_AVAILABLE = True
except ImportError:
    uipath = None  # type: ignore
    UIPATH_AVAILABLE = False


# --------------------------------------------------------------------------
# Namespaces — BPMN 2.0 OMG schema + FDE Agent custom mitigation extension.
# Register only canonical prefixes — registering "" (default) to the same
# URI as "bpmn:" causes ET to pick an arbitrary one at serialization time
# (renderer-dependent quirk).
# --------------------------------------------------------------------------
NS = {
    "bpmn": "http://www.omg.org/spec/BPMN/20100524/MODEL",
    "fde":  "https://fde-agent.io/bpmn/ext",
}
ET.register_namespace("bpmn",   NS["bpmn"])
ET.register_namespace("fde",    NS["fde"])
ET.register_namespace("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI")
ET.register_namespace("dc",     "http://www.omg.org/spec/DD/20100524/DC")
ET.register_namespace("di",     "http://www.omg.org/spec/DD/20100524/DI")
ET.register_namespace("xsi",    "http://www.w3.org/2001/XMLSchema-instance")


def _parse_preserving_comments(bpmn_path: Path) -> ET.ElementTree:
    """Parse XML while preserving <!-- comments -->.

    `xml.etree.ElementTree` strips comments by default. For audit-grade
    BPMN patching we must round-trip the documentation comments
    (9-step pipeline header, ambiguity resolution rationale, etc.) so
    downstream readers/maintainers retain context. Python 3.8+ supports
    `TreeBuilder(insert_comments=True)` for this.
    """
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.parse(bpmn_path, parser=parser)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BPMN = SCRIPT_DIR / "bpmn_diagnosis_workflow.xml"


# --------------------------------------------------------------------------
# Element lookup — search BPMN flow objects by either element id or
# documentation/name suffix (e.g. "N7" matches a node whose name encodes
# "N7" or whose documentation tags it as such).
# --------------------------------------------------------------------------

def _iter_process_elements(root: ET.Element) -> Iterable[ET.Element]:
    """Yield every flow object inside <bpmn:process>."""
    for proc in root.iter(f"{{{NS['bpmn']}}}process"):
        for child in proc:
            yield child


def _find_target(root: ET.Element, node_id: str) -> ET.Element:
    """Locate a BPMN flow object by element id, then fall back to name match."""
    by_id = root.find(f".//*[@id='{node_id}']")
    if by_id is not None:
        return by_id

    needle = node_id.lower()
    for el in _iter_process_elements(root):
        name = (el.get("name") or "").lower()
        if needle in name:
            return el
        doc = el.find(f"{{{NS['bpmn']}}}documentation")
        if doc is not None and doc.text and needle in doc.text.lower():
            return el

    raise ValueError(
        f"BPMN node {node_id!r} not found (searched element id, name, documentation)."
    )


# --------------------------------------------------------------------------
# Mitigation stamping — append a fde:mitigation child + comma-joined attr
# --------------------------------------------------------------------------

_FDE_MITIGATION_TAG = f"{{{NS['fde']}}}mitigation"


def _stamp_mitigation(
    target: ET.Element,
    option_id: str,
    option_label: str,
    approver_id: str | None = None,
) -> None:
    """Append an fde:mitigation child + maintain `fde:applied_options` attr.

    `<fde:mitigation>` is a structured trace entry (audit-grade). The
    attribute is a flat comma-separated list — easier for downstream
    aggregator queries.
    """
    attr_key = f"{{{NS['fde']}}}applied_options"
    existing = target.get(attr_key, "")
    applied = [x for x in existing.split(",") if x]
    if option_id not in applied:
        applied.append(option_id)
    target.set(attr_key, ",".join(applied))

    entry = ET.SubElement(target, _FDE_MITIGATION_TAG)
    entry.set("option_id", option_id)
    # Timezone-aware UTC for audit trail (EU AI Act Annex III + FSS inspection
    # cross-jurisdiction time consistency).
    entry.set("applied_at",
              _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
    if approver_id:
        entry.set("approver_id", approver_id)
    entry.text = option_label


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def apply_mitigation(
    bpmn_path: Path,
    node_id: str,
    option_id: str,
    option_label: str,
    output_path: Path | None = None,
    approver_id: str | None = None,
    redeploy: bool = False,
) -> dict:
    """Patch a BPMN flow object with a mitigation option.

    Returns a dict with:
        updated_xml_path: Path written to disk
        node_element_id:  resolved BPMN id (may differ from input node_id)
        applied_options:  full comma-separated list after the patch
        redeploy_status:  "stubbed" | "ok" | "skipped"
    """
    bpmn_path = Path(bpmn_path).resolve()
    if not bpmn_path.exists():
        raise FileNotFoundError(f"BPMN file not found: {bpmn_path}")

    tree = _parse_preserving_comments(bpmn_path)
    root = tree.getroot()

    target = _find_target(root, node_id)
    _stamp_mitigation(target, option_id, option_label, approver_id=approver_id)

    out = output_path or bpmn_path.with_suffix(".applied.xml")
    out = Path(out).resolve()
    tree.write(out, xml_declaration=True, encoding="utf-8")

    redeploy_status = _maybe_redeploy(out) if redeploy else "skipped"

    return {
        "updated_xml_path": str(out),
        "node_element_id":   target.get("id"),
        "applied_options":   target.get(f"{{{NS['fde']}}}applied_options", ""),
        "redeploy_status":   redeploy_status,
        "uipath_sdk_available": UIPATH_AVAILABLE,
    }


def _maybe_redeploy(updated_xml: Path) -> str:
    """Trigger UiPath Maestro re-deploy.

    Stubbed until Labs access lands. Wire path post-access:
        - uipath.maestro.workflows.create_revision(file=updated_xml, version_bump='minor')
        - or REST: POST /maestro/v1/workflows/{id}/revisions  (multipart)
    """
    if not UIPATH_AVAILABLE:
        return "stubbed (uipath SDK not installed)"
    # TODO(uipath-access): replace stub with actual SDK call when authenticated.
    return "stubbed (SDK installed but wire pending)"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--bpmn", type=Path, default=DEFAULT_BPMN,
                   help="Input BPMN XML path. Default: bpmn_diagnosis_workflow.xml")
    p.add_argument("--node", type=str, default=None,
                   help="Target node id (BPMN element id, or name/docs substring).")
    p.add_argument("--option", type=str, default=None,
                   help="Mitigation option id (e.g. 'must_fix:fair_lending_audit').")
    p.add_argument("--label", type=str, default="",
                   help="Human-readable option label (free text).")
    p.add_argument("--approver", type=str, default=None,
                   help="Approver id (consultant_1042) for audit trail.")
    p.add_argument("--output", type=Path, default=None,
                   help="Output path. Default: <input>.applied.xml")
    p.add_argument("--redeploy", action="store_true",
                   help="Attempt UiPath Maestro re-deploy (stubbed until SDK access).")
    p.add_argument("--self-test", action="store_true",
                   help="Dry-run against bpmn_diagnosis_workflow.xml / Task_RiskDiag_Security.")
    return p


def _main() -> int:
    args = _build_argparser().parse_args()

    if args.self_test:
        result = apply_mitigation(
            bpmn_path=DEFAULT_BPMN,
            node_id="Task_RiskDiag_Security",
            option_id="must_fix:fair_lending_audit",
            option_label="Fair Lending audit gate + ACS feature attribution (self-test)",
            output_path=DEFAULT_BPMN.with_suffix(".selftest.xml"),
            approver_id="self_test_runner",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        # Cleanup self-test artifact
        Path(result["updated_xml_path"]).unlink(missing_ok=True)
        print("(self-test cleanup: removed", result["updated_xml_path"], ")")
        return 0

    missing = [k for k in ("node", "option") if getattr(args, k) is None]
    if missing:
        print(f"error: missing required args: {missing}", file=sys.stderr)
        return 2

    result = apply_mitigation(
        bpmn_path=args.bpmn,
        node_id=args.node,
        option_id=args.option,
        option_label=args.label,
        output_path=args.output,
        approver_id=args.approver,
        redeploy=args.redeploy,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
