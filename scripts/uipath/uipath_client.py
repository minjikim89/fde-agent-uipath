"""
UiPath Orchestrator HTTP client (FDE Agent — Sprint 4/5 wire)
================================================================

stdlib-only client for the **staging.uipath.com** UiPath Orchestrator
environment used during the AgentHack tenant context (organization code
read from Keychain `uipath_base_url` — never embedded in source).

Design constraints (★ MUST observe):
    1. Environment is **staging.uipath.com**, not cloud.uipath.com.
       The base URL is read from Keychain — never hard-coded here.
    2. **Cloudflare 1010 bypass** — every HTTP request to UiPath endpoints
       MUST include a browser-style User-Agent header. Missing UA returns
       HTTP 403 with `error code: 1010`.
    3. **Credentials come from macOS Keychain only**.
       `security find-generic-password -s <service> -w`
         - uipath_base_url      (required)
         - uipath_client_id     (required)
         - uipath_client_secret (required)
         - uipath_tenant        (required for Tasks API; optional for token only)
    4. **No secret logging**. The token value, client_id, and client_secret
       MUST NOT appear in stdout/stderr/exception messages.
    5. **No `requests` dependency** — stdlib `urllib.request` only.

Auth flow (client_credentials grant):
    POST {base_url}/identity_/connect/token
    Content-Type: application/x-www-form-urlencoded
    grant_type=client_credentials
    &client_id=<…>
    &client_secret=<…>
    &scope=<space-separated scopes>
    User-Agent: <browser-style>

Token cache: in-memory only (Sprint 5: process-lifetime). 60s safety margin
before expiry. Re-issue on demand.

Tasks API (Action Center HITL):
    GET  {base}/{tenant}/orchestrator_/odata/Tasks?$top=N
    POST {base}/{tenant}/orchestrator_/tasks/GenericTasks/CreateTask
    Authorization: Bearer <token>
    X-UIPATH-OrganizationUnitId: <folder_id>   (when scoped to folder)

By default Tasks-API write methods (`create_generic_task`) are **dry-run**.
The dry-run path returns the would-be request payload + endpoint, never
hits the network. Real submission requires `dry_run=False` AND the
tenant Keychain entry present AND explicit confirmation from the main
session per project negative rules.

Usage:
    from uipath_client import UiPathClient
    client = UiPathClient()
    token_info = client.healthcheck()         # safe — no Tasks API call
    print(token_info["status"])               # "ok" or "error"
    # tenant-gated:
    tasks = client.list_tasks(top=5)          # raises if tenant missing
    payload = client.create_generic_task(...) # dry-run by default
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


# --------------------------------------------------------------------------
# Constants — ★ no hard-coded URLs / secrets / tenant names
# --------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_SCOPES = (
    "OR.Tasks OR.Tasks.Read OR.Tasks.Write "
    "OR.Execution OR.Execution.Read OR.Execution.Write "
    "OR.Folders OR.Folders.Read OR.Folders.Write "
    "OR.Jobs OR.Jobs.Read OR.Jobs.Write"
)

TOKEN_PATH = "/identity_/connect/token"
TOKEN_REFRESH_MARGIN_SEC = 60   # re-issue this many seconds before expiry
REQUEST_TIMEOUT_SEC = 30

KEYCHAIN_KEYS = {
    "base_url":      "uipath_base_url",
    "client_id":     "uipath_client_id",
    "client_secret": "uipath_client_secret",
    "tenant":        "uipath_tenant",   # optional at construction
}


# --------------------------------------------------------------------------
# Errors — leak no secrets via message
# --------------------------------------------------------------------------

class UiPathConfigError(RuntimeError):
    """Raised when a required Keychain entry is missing or empty."""


class UiPathAuthError(RuntimeError):
    """Token issuance failed. Message must not include client_id/secret."""


class UiPathRequestError(RuntimeError):
    """Non-2xx response from an authenticated UiPath endpoint."""


# --------------------------------------------------------------------------
# Keychain access — secret-safe read only
# --------------------------------------------------------------------------

def _keychain_read(service: str) -> str | None:
    """Read a generic-password entry by service. Returns None if missing.

    Uses `security find-generic-password -s <service> -w` so we never
    inspect the account name. The secret value is returned in-memory and
    must not be logged downstream.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").rstrip("\n")
    return value if value else None


# --------------------------------------------------------------------------
# Token cache — in-memory only (process lifetime)
# --------------------------------------------------------------------------

@dataclass
class _CachedToken:
    access_token: str = ""
    expires_at_unix: float = 0.0
    scope: str = ""
    token_type: str = "Bearer"

    def fresh(self) -> bool:
        return bool(self.access_token) and time.time() < (
            self.expires_at_unix - TOKEN_REFRESH_MARGIN_SEC
        )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

@dataclass
class UiPathClient:
    """Lightweight Orchestrator client.

    Cred is loaded from Keychain at construction. `tenant` is optional —
    org-level / token-only calls work without it, but Tasks API and any
    `orchestrator_/odata/...` route requires a tenant in the URL.
    """

    user_agent: str = DEFAULT_USER_AGENT
    scopes: str = DEFAULT_SCOPES
    _base_url: str = field(init=False, default="")
    _client_id: str = field(init=False, default="")
    _client_secret: str = field(init=False, default="")
    _tenant: str | None = field(init=False, default=None)
    _token: _CachedToken = field(init=False, default_factory=_CachedToken)

    def __post_init__(self) -> None:
        base = _keychain_read(KEYCHAIN_KEYS["base_url"])
        cid = _keychain_read(KEYCHAIN_KEYS["client_id"])
        sec = _keychain_read(KEYCHAIN_KEYS["client_secret"])
        if not base:
            raise UiPathConfigError(
                f"Keychain entry {KEYCHAIN_KEYS['base_url']!r} missing or empty"
            )
        if not cid or not sec:
            raise UiPathConfigError(
                "Keychain entries for client_id / client_secret missing or empty"
            )
        # Strip trailing slash so concatenation never doubles up.
        self._base_url = base.rstrip("/")
        self._client_id = cid
        self._client_secret = sec
        self._tenant = _keychain_read(KEYCHAIN_KEYS["tenant"])
        # Hardcoded staging guard — if someone overwrites Keychain with the
        # cloud production URL, refuse before any request hits the wire.
        if "cloud.uipath.com" in self._base_url:
            raise UiPathConfigError(
                "base_url points to cloud.uipath.com — hackathon env is staging.uipath.com only"
            )

    # ----- public ---------------------------------------------------------

    @property
    def tenant(self) -> str | None:
        return self._tenant

    @property
    def has_tenant(self) -> bool:
        return bool(self._tenant)

    @property
    def base_url(self) -> str:
        return self._base_url

    def healthcheck(self) -> dict:
        """Issue (or reuse) a token + return a sanitized summary.

        Does NOT print or return the token value, the tenant path, or the
        organization code. Useful as a smoke test before any Tasks API call.
        """
        try:
            self._ensure_token()
        except UiPathAuthError as e:
            return {"status": "error", "stage": "token", "error": str(e)}
        except UiPathConfigError as e:
            return {"status": "error", "stage": "config", "error": str(e)}
        return {
            "status":         "ok",
            "stage":          "token",
            "host_only":      _host_only(self._base_url),    # netloc only, no org path
            "tenant_present": self.has_tenant,
            "scope_granted":  self._token.scope,
            "token_type":     self._token.token_type,
            "expires_in_sec": max(0, int(self._token.expires_at_unix - time.time())),
        }

    # ----- Tasks API (Action Center HITL) ---------------------------------

    def list_tasks(self, top: int = 10, folder_id: int | None = None) -> dict:
        """GET {base}/{tenant}/orchestrator_/odata/Tasks?$top=N

        Requires tenant. No body, no side effect — safe to call.
        """
        self._require_tenant("list_tasks")
        path = f"/{self._tenant}/orchestrator_/odata/Tasks"
        params = {"$top": str(top)}
        return self._authed_request(
            "GET", path, params=params, folder_id=folder_id,
        )

    def create_generic_task(
        self,
        title: str,
        priority: str = "Medium",
        data: dict | None = None,
        folder_id: int | None = None,
        assigned_to_user_id: int | None = None,
        dry_run: bool = True,
    ) -> dict:
        """POST {base}/{tenant}/orchestrator_/tasks/GenericTasks/CreateTask

        Used by Loop B-Symbolic to surface FDE Agent diagnosis result as an
        Action Center generic task for human review (HITL).

        Args:
            title: human-readable Action Center title (max 256 chars).
            priority: "Low" | "Medium" | "High" | "Critical".
            data: free-form JSON payload (rendered as form data in Action
                Center). Typically the `hitl_reason` + `diagnoses[]` slice
                from coded_agent_wrapper.run().
            folder_id: Orchestrator folder (OrganizationUnit) id.
            assigned_to_user_id: optional assignee.
            dry_run: when True (default), returns the would-be request
                payload + endpoint without hitting the network.

        Returns:
            dict with `endpoint`, `request_body`, `method`. If dry_run is
            False and the call succeeded, `response` is included.
        """
        self._require_tenant("create_generic_task")
        path = f"/{self._tenant}/orchestrator_/tasks/GenericTasks/CreateTask"
        body: dict[str, Any] = {
            "title":    title[:256],
            "priority": priority,
            "data":     data or {},
        }
        if assigned_to_user_id is not None:
            body["assignedToUserId"] = assigned_to_user_id

        if dry_run:
            return {
                "dry_run":      True,
                "method":       "POST",
                "endpoint":     self._base_url + path,
                "request_body": body,
                "folder_id":    folder_id,
                "note":         "Set dry_run=False after main-session confirmation to actually submit.",
            }

        response = self._authed_request(
            "POST", path, json_body=body, folder_id=folder_id,
        )
        return {
            "dry_run":      False,
            "method":       "POST",
            "endpoint":     self._base_url + path,
            "request_body": body,
            "folder_id":    folder_id,
            "response":     response,
        }

    def submit_diagnosis_for_hitl(
        self,
        diagnosis_result: dict,
        bpmn_workflow_id: str = "",
        folder_id: int | None = None,
        dry_run: bool = True,
    ) -> dict:
        """Surface a coded_agent_wrapper.run() result as an Action Center task.

        Maps the FDE Agent diagnosis payload to the GenericTasks/CreateTask
        shape that the Action Center HITL form expects (Hero Moment scene
        Beat (d) Dossier rendering). Truncates the diagnoses list to keep
        the form payload under the Orchestrator 256KB request cap; the
        full report is referenced via `report_paths`.

        Dry-run by default — actual submission requires main-session
        confirmation (project Don't rule).
        """
        diagnoses = diagnosis_result.get("diagnoses") or []
        max_score = diagnosis_result.get("max_final_score") or 0.0
        sample_name = diagnosis_result.get("sample_name") or "unknown"
        title = (
            f"[FDE] {sample_name} pre-deployment diagnosis "
            f"— max {max_score:.2f} ({len(diagnoses)} nodes)"
        )
        data = {
            "hitl_reason":      diagnosis_result.get("hitl_reason", ""),
            "max_final_score":  max_score,
            "runtime_alerts":   diagnosis_result.get("runtime_alerts", 0),
            "diagnoses":        diagnoses[:10],   # cap to first 10 RED nodes
            "report_paths":     diagnosis_result.get("report_paths", {}),
            "bpmn_workflow_id": bpmn_workflow_id,
        }
        priority = "High" if max_score >= 4.5 else "Medium"
        return self.create_generic_task(
            title=title,
            priority=priority,
            data=data,
            folder_id=folder_id,
            dry_run=dry_run,
        )

    # ----- internals ------------------------------------------------------

    def _require_tenant(self, op: str) -> None:
        if not self.has_tenant:
            raise UiPathConfigError(
                f"{op}: tenant not configured. "
                f"Set Keychain entry {KEYCHAIN_KEYS['tenant']!r} once the tenant name is confirmed."
            )

    def _ensure_token(self) -> None:
        if self._token.fresh():
            return
        body = urllib.parse.urlencode({
            "grant_type":    "client_credentials",
            "client_id":     self._client_id,
            "client_secret": self._client_secret,
            "scope":         self.scopes,
        }).encode("utf-8")
        url = self._base_url + TOKEN_PATH
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept":       "application/json",
                "User-Agent":   self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Sanitize: drop response body since UiPath can echo client_id.
            raise UiPathAuthError(
                f"token issuance HTTP {e.code} from {_mask_host(url)}"
            ) from None
        except urllib.error.URLError as e:
            raise UiPathAuthError(
                f"token issuance network error to {_mask_host(url)}: {e.reason}"
            ) from None

        access = payload.get("access_token")
        if not access:
            raise UiPathAuthError("token response missing access_token")
        self._token = _CachedToken(
            access_token=access,
            expires_at_unix=time.time() + int(payload.get("expires_in", 3600)),
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "Bearer"),
        )

    def _authed_request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        folder_id: int | None = None,
    ) -> dict:
        self._ensure_token()
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {
            "Authorization": f"{self._token.token_type} {self._token.access_token}",
            "Accept":        "application/json",
            "User-Agent":    self.user_agent,
        }
        if folder_id is not None:
            headers["X-UIPATH-OrganizationUnitId"] = str(folder_id)
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise UiPathRequestError(
                f"{method} {_mask_host(url)} → HTTP {e.code}"
            ) from None
        except urllib.error.URLError as e:
            raise UiPathRequestError(
                f"{method} {_mask_host(url)} network error: {e.reason}"
            ) from None


# --------------------------------------------------------------------------
# Sanitizer — strip query string and credentials from URL for log lines
# --------------------------------------------------------------------------

def _mask_host(url: str) -> str:
    """Strip query string + credentials. Path retained for request-error context."""
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _host_only(url: str) -> str:
    """Return netloc only — used by healthcheck output (no org/tenant disclosure)."""
    parsed = urllib.parse.urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


__all__ = [
    "UiPathClient",
    "UiPathConfigError",
    "UiPathAuthError",
    "UiPathRequestError",
    "DEFAULT_USER_AGENT",
    "DEFAULT_SCOPES",
    "KEYCHAIN_KEYS",
]
