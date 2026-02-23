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
import os
import time

import requests

# Public OAuth App client-id used by GitHub Copilot CLI / open-source clients.
GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"

# Where the GitHub PAT is cached between sessions.
TOKEN_FILE = os.path.expanduser("~/.copilot_chatbot_token.json")


# ---------------------------------------------------------------------------
# Device-flow helpers
# ---------------------------------------------------------------------------

def request_device_code() -> dict:
    """Request a device code from GitHub and return the full JSON payload."""
    response = requests.post(
        "https://github.com/login/device/code",
        headers={"Accept": "application/json"},
        data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
        timeout=15,
    )
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
            return data["access_token"]

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
    response = requests.get(
        "https://api.github.com/copilot_internal/v2/token",
        headers={
            "Authorization": f"token {github_token}",
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()
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
