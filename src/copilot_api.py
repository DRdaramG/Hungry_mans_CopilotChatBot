"""
GitHub Copilot Chat API client.

Mimics the HTTP traffic produced by the VS Code Copilot Chat extension so that
the Copilot back-end accepts the requests from a standalone Python application.
The Copilot token is refreshed automatically ~60 s before it expires.
"""

import json
import logging
import time
from typing import Generator, Iterator

import requests

from .auth import get_copilot_token
from .context_manager import build_context_window

log = logging.getLogger("copilot_chatbot")

COPILOT_CHAT_URL = "https://api.githubcopilot.com/chat/completions"

# ---------------------------------------------------------------------------
# Available models
# Keys are human-readable display names shown in the UI toggle.
# Values are the model identifiers sent to the Copilot API.
# ---------------------------------------------------------------------------
MODELS: dict[str, str] = {
    "Claude Opus 4.5": "claude-opus-4-5",
    "Gemini 1.5 Pro":  "gemini-1.5-pro",
    "GPT-4o":          "gpt-4o",
}

# Headers that mimic VS Code's Copilot Chat extension.
_COPILOT_HEADERS = {
    "Content-Type":           "application/json",
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version":         "vscode/1.97.0",
    "Editor-Plugin-Version":  "copilot-chat/0.22.2",
    "User-Agent":             "GitHubCopilotChat/0.22.2",
    "openai-intent":          "conversation-panel",
    "x-github-api-version":   "2023-07-07",
}


class CopilotClient:
    """Thin wrapper around the Copilot Chat completions endpoint."""

    def __init__(self, github_token: str) -> None:
        self._github_token = github_token
        self._copilot_token: str = ""
        self._token_expires_at: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Refresh the Copilot bearer token when it is about to expire."""
        now = time.time()
        if now >= self._token_expires_at - 60:
            log.debug("[API] Copilot token expired or missing (now=%.0f, "
                      "expires=%.0f). Refreshing…", now, self._token_expires_at)
            self._copilot_token, self._token_expires_at = get_copilot_token(
                self._github_token
            )
            log.debug("[API] New Copilot token valid until %d",
                      self._token_expires_at)

    @staticmethod
    def _parse_sse(response: requests.Response) -> Generator[str, None, None]:
        """
        Yield text deltas from a server-sent-events (SSE) stream.

        Each non-empty line has the form ``data: <json>``; ``data: [DONE]``
        signals the end of the stream.
        """
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line: str = (
                raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            )
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"].get("content", "")
                if delta:
                    yield delta
            except (json.JSONDecodeError, KeyError, IndexError):
                continue

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
        stream: bool = True,
    ) -> Iterator[str] | str:
        """
        Send *messages* to the Copilot chat completions endpoint.

        Parameters
        ----------
        messages : OpenAI-style list of ``{"role": ..., "content": ...}`` dicts.
        model    : Copilot model identifier (see :data:`MODELS`).
        stream   : When *True* (default) returns a generator of text deltas;
                   when *False* returns the full response as a plain string.
        """
        self._ensure_token()

        # --- Debug: show what is actually being sent ---
        log.debug("[API] ── Sending chat request ──")
        log.debug("[API]   model = %s  |  stream = %s", model, stream)
        log.debug("[API]   message count = %d", len(messages))
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            preview = (content[:120] + "…") if isinstance(content, str) and len(content) > 120 else content
            if isinstance(content, list):
                preview = f"[multipart, {len(content)} parts]"
            log.debug("[API]   msg[%d] role=%-10s  content=%s", i, role, preview)
        log.debug("[API] ─────────────────────────")

        # Trim the message list to fit within the context window before
        # sending.  build_context_window() raises ValueError when the
        # current user message alone is too large; that exception
        # propagates up to the caller for user-facing display.
        trimmed = build_context_window(messages)
        if len(trimmed) < len(messages):
            log.info(
                "[API] Message list trimmed from %d to %d messages "
                "to fit context window.",
                len(messages),
                len(trimmed),
            )

        headers = {
            **_COPILOT_HEADERS,
            "Authorization": f"Bearer {self._copilot_token}",
        }
        payload = {
            "model":       model,
            "messages":    trimmed,
            "stream":      stream,
            "n":           1,
            "top_p":       1,
            "temperature": 0.1,
        }

        response = requests.post(
            COPILOT_CHAT_URL,
            headers=headers,
            json=payload,
            stream=stream,
            timeout=120,
        )
        response.raise_for_status()

        if stream:
            return self._parse_sse(response)

        return response.json()["choices"][0]["message"]["content"]
