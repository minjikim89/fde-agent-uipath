"""
UiPath Orchestrator Credential Healthcheck (FDE Agent — Sprint 4/5)
====================================================================

End-to-end credential validation against the staging.uipath.com
environment WITHOUT exposing any secret values.

What it checks:
    1. All required Keychain entries are present (uipath_base_url,
       uipath_client_id, uipath_client_secret). Tenant entry is reported
       but not required at this stage.
    2. The base_url is the staging host (cloud.uipath.com is rejected).
    3. Token issuance succeeds (POST /identity_/connect/token,
       client_credentials grant). Verifies User-Agent header bypasses
       Cloudflare 1010.
    4. Token response includes scope + expires_in.

What it intentionally does NOT do:
    - Print or return the access_token, client_id, or client_secret.
    - Call any Tasks-API endpoint (those require tenant + main-session
      confirmation per project negative rules).
    - Submit any GenericTask (dry-run only via uipath_client.create_generic_task).

Usage:
    python3 healthcheck.py
    python3 healthcheck.py --json

Exit codes:
    0 — all checks passed (or partial pass with tenant missing-but-token-OK)
    1 — token issuance failed or Keychain entry missing
"""
from __future__ import annotations

import argparse
import json as _json
import sys

from uipath_client import (
    KEYCHAIN_KEYS,
    UiPathAuthError,
    UiPathClient,
    UiPathConfigError,
    _host_only,
    _keychain_read,
)


def _check_keychain() -> dict:
    """Report Keychain presence without printing values."""
    required = ("base_url", "client_id", "client_secret")
    optional = ("tenant",)
    presence = {}
    missing = []
    for key in required + optional:
        service = KEYCHAIN_KEYS[key]
        value = _keychain_read(service)
        presence[service] = bool(value)
        if key in required and not value:
            missing.append(service)
    return {
        "presence": presence,
        "missing_required": missing,
    }


def _check_token(client: UiPathClient) -> dict:
    """Issue a token + return sanitized info (no token value)."""
    summary = client.healthcheck()
    return summary


def run() -> dict:
    report: dict = {
        "stage_1_keychain": {},
        "stage_2_staging_guard": {},
        "stage_3_token":  {},
        "overall_status": "pending",
    }

    # Stage 1 — Keychain presence
    keychain = _check_keychain()
    report["stage_1_keychain"] = keychain
    if keychain["missing_required"]:
        report["overall_status"] = "error"
        report["error"] = f"missing Keychain entries: {keychain['missing_required']}"
        return report

    # Stage 2 — Staging-only guard (also enforced inside UiPathClient.__post_init__)
    try:
        client = UiPathClient()
    except UiPathConfigError as e:
        report["stage_2_staging_guard"] = {"status": "error", "error": str(e)}
        report["overall_status"] = "error"
        return report
    report["stage_2_staging_guard"] = {
        "status": "ok",
        "host_only": _host_only(client.base_url),
    }

    # Stage 3 — Token issuance (network call to staging)
    token_summary = _check_token(client)
    report["stage_3_token"] = token_summary
    if token_summary["status"] != "ok":
        report["overall_status"] = "error"
        return report

    # Pass — token issued. Tenant may still be unset (downstream Tasks API
    # will refuse until configured), which we report as a flag, not an error.
    report["overall_status"] = "ok"
    report["tenant_flag"] = (
        "tenant entry present — Tasks API ready"
        if client.has_tenant
        else "tenant entry missing — set Keychain 'uipath_tenant' once confirmed"
    )
    return report


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--json", action="store_true",
                   help="emit the report as a single-line JSON document")
    args = p.parse_args(argv)

    try:
        report = run()
    except UiPathAuthError as e:
        report = {"overall_status": "error", "stage_3_token": {"status": "error", "error": str(e)}}

    if args.json:
        print(_json.dumps(report, ensure_ascii=False))
    else:
        _pretty_print(report)

    return 0 if report.get("overall_status") == "ok" else 1


def _pretty_print(report: dict) -> None:
    print("UiPath Credential Healthcheck")
    print("=" * 40)
    s1 = report.get("stage_1_keychain", {})
    print("\n[1] Keychain entries (presence only):")
    for svc, present in s1.get("presence", {}).items():
        glyph = "✓" if present else "✗"
        print(f"   {glyph} {svc}")
    if s1.get("missing_required"):
        print(f"   ! missing required: {s1['missing_required']}")

    s2 = report.get("stage_2_staging_guard", {})
    if s2:
        print("\n[2] Staging guard:")
        print(f"   status: {s2.get('status', 'n/a')}")
        if s2.get("host_only"):
            print(f"   host  : {s2['host_only']}")
        if s2.get("error"):
            print(f"   error : {s2['error']}")

    s3 = report.get("stage_3_token", {})
    if s3:
        print("\n[3] Token issuance:")
        print(f"   status        : {s3.get('status', 'n/a')}")
        if s3.get("status") == "ok":
            print(f"   token_type    : {s3.get('token_type', 'n/a')}")
            print(f"   expires_in_sec: {s3.get('expires_in_sec', 'n/a')}")
            print(f"   tenant_present: {s3.get('tenant_present', False)}")
            scope = s3.get("scope_granted", "")
            print(f"   scopes (count): {len(scope.split()) if scope else 0}")
        else:
            print(f"   error         : {s3.get('error', 'n/a')}")

    print("\n[overall]:", report.get("overall_status"))
    if report.get("tenant_flag"):
        print("[tenant]:", report["tenant_flag"])
    if report.get("error"):
        print("[error] :", report["error"])


if __name__ == "__main__":
    sys.exit(_main())
