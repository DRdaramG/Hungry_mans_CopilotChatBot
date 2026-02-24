"""
GitHub OAuth device-flow authentication for GitHub Copilot.

Flow:
  1. Request a device code from GitHub (using the public Copilot client-id).
  2. Display the user-code and verification URL so the user can authorise in
     their browser.
  3. Poll GitHub until the user authorises (or cancels / token expires).
  4. Exchange the resulting GitHub PAT for a short-lived Copilot API token.
  5. Persist the GitHub PAT to disk so subsequent launches skip step 1-3.
"""

import json
import logging
import os
import time

import requests

from .paths import asset_path

# ---------------------------------------------------------------------------
# Debug logger — prints to console so the user can see exactly what happens.
# ---------------------------------------------------------------------------
log = logging.getLogger("copilot_chatbot")

# Public OAuth App client-id used by GitHub Copilot CLI / open-source clients.
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"

# Where the GitHub PAT is cached between sessions.
TOKEN_FILE = asset_path("token.json")


# ---------------------------------------------------------------------------
# Device-flow helpers
# ---------------------------------------------------------------------------

def request_device_code() -> dict:
    """Request a device code from GitHub and return the full JSON payload."""
    url = "https://github.com/login/device/code"
    data = {"client_id": GITHUB_CLIENT_ID}
    log.debug("[AUTH] POST %s  client_id=%s", url, GITHUB_CLIENT_ID)
    response = requests.post(
        url,
        headers={"Accept": "application/json"},
        data=data,
        timeout=15,
    )
    log.debug("[AUTH] Device-code response: %s %s", response.status_code,
              response.text[:500])
    response.raise_for_status()
    return response.json()


def poll_for_token(device_code: str, interval: int = 5,
                   is_cancelled=None) -> str | None:
    """
    Poll GitHub until the user completes device-flow authorisation.

    Parameters
    ----------
    device_code  : GitHub device_code value from :func:`request_device_code`.
    interval     : Polling interval in seconds (defaults to the value returned
                   by GitHub, typically 5 s).
    is_cancelled : Optional zero-argument callable; polling stops and ``None``
                   is returned when it evaluates to *True*.

    Returns
    -------
    The GitHub personal-access token, or ``None`` if cancelled.
    """
    while True:
        if is_cancelled and is_cancelled():
            return None

        time.sleep(interval)

        if is_cancelled and is_cancelled():
            return None

        response = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15,
        )
        data = response.json()

        if "access_token" in data:
            tok = data["access_token"]
            log.debug("[AUTH] GitHub token obtained — type=%s  len=%d  prefix=%s…",
                      data.get("token_type", "?"), len(tok), tok[:8])
            return tok

        error = data.get("error", "")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            interval += 5
        elif error == "expired_token":
            raise RuntimeError(
                "Device code has expired. Please restart authentication."
            )
        elif error == "access_denied":
            raise RuntimeError("Authorisation denied by user.")
        else:
            raise RuntimeError(
                data.get("error_description", error) or "Unknown error"
            )


# ---------------------------------------------------------------------------
# Copilot token exchange
# ---------------------------------------------------------------------------

def get_copilot_token(github_token: str) -> tuple[str, int]:
    """
    Exchange a GitHub PAT for a short-lived Copilot API bearer token.

    Returns
    -------
    (copilot_token, expires_at_unix_timestamp)
    """
    url = "https://api.github.com/copilot_internal/v2/token"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/json",
        "User-Agent":    "GitHubCopilotChat/0.22.2",
        "Editor-Version": "vscode/1.97.0",
        "Editor-Plugin-Version": "copilot-chat/0.22.2",
    }

    log.debug("[AUTH] GET %s", url)
    log.debug("[AUTH]   token prefix = %s…  len = %d",
              github_token[:8], len(github_token))

    response = requests.get(url, headers=headers, timeout=15)

    log.debug("[AUTH] Copilot token-exchange response: %s", response.status_code)
    log.debug("[AUTH]   response headers: %s",
              {k: v for k, v in response.headers.items()
               if k.lower() in ("x-github-request-id", "x-oauth-scopes",
                                "x-accepted-oauth-scopes", "content-type")})
    log.debug("[AUTH]   response body (first 800 chars): %s",
              response.text[:800])

    if response.status_code == 403:
        body = response.text
        raise RuntimeError(
            f"Copilot token exchange failed (HTTP 403).\n"
            f"Response: {body}\n\n"
            f"Possible causes:\n"
            f"  • GitHub Copilot Pro+ subscription may not be active\n"
            f"  • The OAuth token may lack Copilot scopes\n"
            f"  • The OAuth App client-id may be outdated"
        )

    response.raise_for_status()
    data = response.json()
    log.debug("[AUTH] Copilot bearer token obtained — expires_at=%s",
              data.get("expires_at", "?"))
    return data["token"], int(data.get("expires_at", 0))


# ---------------------------------------------------------------------------
# Persistent token storage
# ---------------------------------------------------------------------------

def save_token(github_token: str) -> None:
    """Persist the GitHub PAT to disk."""
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        json.dump({"github_token": github_token}, fh)


def load_token() -> str | None:
    """Load the GitHub PAT from disk, returning ``None`` if absent."""
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("github_token")
    return None


def delete_token() -> None:
    """Remove the cached GitHub PAT from disk."""
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
