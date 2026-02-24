"""
GitHub Copilot Chat API client.

Mimics the HTTP traffic produced by the VS Code Copilot Chat extension so that
the Copilot back-end accepts the requests from a standalone Python application.
The Copilot token is refreshed automatically ~60 s before it expires.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Generator, Iterator

import requests

from .auth import get_copilot_token
from .context_manager import build_context_window

log = logging.getLogger("copilot_chatbot")


# ---------------------------------------------------------------------------
# Error-handling helpers
# ---------------------------------------------------------------------------

class CopilotAPIError(Exception):
    """Rich API error that preserves diagnostic context for debugging.

    Attributes
    ----------
    status_code : int | None
        HTTP status code (``None`` for non-HTTP errors).
    endpoint : str
        The URL that was called.
    model : str
        Model identifier sent in the request.
    response_body : str
        First 500 chars of the response body (often contains the real error).
    payload_summary : dict | None
        Summarised payload (keys + a few values) for reproducing the issue.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        endpoint: str = "",
        model: str = "",
        response_body: str = "",
        payload_summary: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.model = model
        self.response_body = response_body
        self.payload_summary = payload_summary
        super().__init__(message)

    def __str__(self) -> str:  # noqa: D105
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f"  HTTP {self.status_code}")
        if self.endpoint:
            parts.append(f"  Endpoint: {self.endpoint}")
        if self.model:
            parts.append(f"  Model: {self.model}")
        if self.response_body:
            parts.append(f"  Response: {self.response_body[:500]}")
        if self.payload_summary:
            parts.append(f"  Payload keys: {list(self.payload_summary.keys())}")
        return "\n".join(parts)


def _extract_error_detail(response: requests.Response) -> str:
    """Extract a human-/LLM-readable error description from an HTTP response.

    Tries to parse a JSON body (common for Copilot/OpenAI/Anthropic errors)
    and falls back to the raw text (truncated to 500 chars).
    """
    try:
        body = response.json()
        # OpenAI / Copilot style: {"error": {"message": "...", "type": "..."}}
        if isinstance(body, dict) and "error" in body:
            err = body["error"]
            if isinstance(err, dict):
                return (
                    f"[{err.get('type', 'error')}] "
                    f"{err.get('message', str(err))}"
                )
            return str(err)
        return response.text[:500]
    except Exception:  # noqa: BLE001
        return response.text[:500] if response.text else "(empty body)"


def _summarise_payload(payload: dict) -> dict:
    """Return a compact summary of a request payload for diagnostics.

    Keeps scalar fields intact but replaces long message lists with counts.
    """
    summary = {}
    for k, v in payload.items():
        if k == "messages":
            summary["messages"] = f"[{len(v)} messages]"
        elif k == "system" and isinstance(v, str) and len(v) > 80:
            summary["system"] = v[:80] + "…"
        else:
            summary[k] = v
    return summary

COPILOT_CHAT_URL = "https://api.githubcopilot.com/chat/completions"
COPILOT_CLAUDE_URL = "https://api.githubcopilot.com/v1/messages"
COPILOT_MODELS_URL = "https://api.githubcopilot.com/models"


@dataclass
class ModelLimits:
    """Token limits for a specific Copilot model."""

    max_context_window_tokens: int
    max_prompt_tokens: int
    max_output_tokens: int


# ---------------------------------------------------------------------------
# Available models
# Keys are human-readable display names shown in the UI toggle.
# Values are the model identifiers sent to the Copilot API.
# ---------------------------------------------------------------------------
MODELS: dict[str, str] = {
    "Claude Opus 4.5": "claude-opus-4-5",
    "Gemini 3 Pro":    "gemini-3-pro-preview",
    "GPT-4.1":         "gpt-4.1",
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


# ---------------------------------------------------------------------------
# Model family detection
# ---------------------------------------------------------------------------

def get_model_family(model_id: str) -> str:
    """Return the model family: ``'claude'``, ``'gemini'``, or ``'openai'``."""
    mid = model_id.lower()
    if "claude" in mid:
        return "claude"
    if "gemini" in mid:
        return "gemini"
    return "openai"


# ---------------------------------------------------------------------------
# Model-specific message formatters
# ---------------------------------------------------------------------------

def _format_messages_openai(messages: list[dict]) -> list[dict]:
    """GPT models — standard OpenAI chat format, returned as-is."""
    return messages


def _format_messages_gemini(messages: list[dict]) -> list[dict]:
    """Gemini models — OpenAI-compatible format.

    Gemini is largely compatible but does NOT support ``image_url`` content
    parts through the Copilot proxy.  Images are converted to a text
    placeholder so the rest of the message still arrives.
    """
    out: list[dict] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            # Flatten multipart: keep text parts, replace images with note
            parts: list[dict] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append(part)
                elif part.get("type") == "image_url":
                    parts.append({
                        "type": "text",
                        "text": "[Image attached — Gemini via Copilot proxy "
                                "does not support inline images.]",
                    })
            out.append({**msg, "content": parts})
        else:
            out.append(msg)
    return out


def _format_messages_claude(messages: list[dict]) -> tuple[list[dict], str | None]:
    """Claude models — ensure strict user/assistant alternation.

    Returns ``(messages, system_text)`` where *system_text* is extracted
    from any leading system message (Claude prefers it as a top-level
    ``system`` parameter rather than an in-band message).

    Additional rules enforced:
    * Consecutive messages with the same role are merged into one.
    * Only ``user`` or ``assistant`` roles remain; ``system`` at index 0
      is extracted, any other system messages are prepended to the next
      user message.
    """
    system_text: str | None = None
    body: list[dict] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # --- extract system --------------------------------------------------
        if role == "system":
            text = _flatten_content(content)
            if system_text is None:
                system_text = text
            else:
                system_text += "\n" + text
            continue

        # --- normalise role (tool → assistant, function → assistant) ----------
        if role not in ("user", "assistant"):
            role = "assistant"

        # --- image_url → Claude source format --------------------------------
        if isinstance(content, list):
            content = _convert_image_parts_for_claude(content)

        # --- merge consecutive same-role messages ----------------------------
        if body and body[-1]["role"] == role:
            body[-1] = _merge_messages(body[-1], {"role": role, "content": content})
        else:
            body.append({"role": role, "content": content})

    # Claude requires the conversation to start with a user message.
    if body and body[0]["role"] != "user":
        body.insert(0, {"role": "user", "content": "(start)"})

    return body, system_text


# ---------------------------------------------------------------------------
# Content-conversion helpers (Claude)
# ---------------------------------------------------------------------------

def _flatten_content(content) -> str:
    """Return plain text regardless of whether *content* is str or list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(parts)
    return str(content)


def _convert_image_parts_for_claude(parts: list[dict]) -> list[dict]:
    """Convert OpenAI-style ``image_url`` blocks to Claude ``image`` blocks.

    Claude via the Copilot proxy expects::

        {"type": "image", "source": {"type": "base64",
         "media_type": "image/png", "data": "..."}}

    While OpenAI uses::

        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    """
    converted: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            url: str = part.get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                # Parse  data:<media_type>;base64,<data>
                header, _, b64 = url.partition(",")
                media_type = header.split(":", 1)[-1].split(";", 1)[0]
                converted.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type or "image/png",
                        "data": b64,
                    },
                })
            else:
                # Remote URL — cannot convert; add placeholder
                converted.append({"type": "text", "text": f"[image: {url}]"})
        else:
            converted.append(part)
    return converted


def _merge_messages(a: dict, b: dict) -> dict:
    """Merge two messages with the same role into one."""
    ac = a.get("content", "")
    bc = b.get("content", "")
    if isinstance(ac, str) and isinstance(bc, str):
        return {"role": a["role"], "content": ac + "\n" + bc}
    # At least one is multipart list
    def _as_list(c):
        if isinstance(c, list):
            return c
        return [{"type": "text", "text": c}] if c else []
    return {"role": a["role"], "content": _as_list(ac) + _as_list(bc)}


# ---------------------------------------------------------------------------
# Model-specific payload builders
# ---------------------------------------------------------------------------

def _build_payload_openai(
    messages: list[dict], model: str, stream: bool,
) -> dict:
    """Build request payload for GPT models."""
    return {
        "model":       model,
        "messages":    messages,
        "stream":      stream,
        "n":           1,
        "top_p":       1,
        "temperature": 0.1,
    }


def _build_payload_gemini(
    messages: list[dict], model: str, stream: bool,
) -> dict:
    """Build request payload for Gemini models."""
    return {
        "model":       model,
        "messages":    messages,
        "stream":      stream,
        "n":           1,
        "top_p":       1,
        "temperature": 0.1,
    }


def _build_payload_claude(
    messages: list[dict],
    model: str,
    stream: bool,
    system_text: str | None = None,
    max_output_tokens: int = 16_000,
) -> dict:
    """Build request payload for Claude models (Anthropic Messages API).

    Claude-specific differences:
    * ``max_tokens`` is **required** (output token limit).
    * ``n`` is NOT supported.
    * ``top_p`` should NOT be combined with ``temperature``.
    * ``system`` can be passed as a top-level string parameter.
    """
    payload: dict = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_output_tokens,
        "stream":      stream,
        "temperature": 0.1,
    }
    if system_text:
        payload["system"] = system_text
    return payload


class CopilotClient:
    """Thin wrapper around the Copilot Chat completions endpoint."""

    def __init__(self, github_token: str) -> None:
        self._github_token = github_token
        self._copilot_token: str = ""
        self._token_expires_at: int = 0
        self._model_limits: dict[str, ModelLimits] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Refresh the Copilot bearer token when it is about to expire."""
        now = time.time()
        if now >= self._token_expires_at - 60:
            log.debug("[API] Copilot token expired or missing (now=%.0f, "
                      "expires=%.0f). Refreshing…", now, self._token_expires_at)
            try:
                self._copilot_token, self._token_expires_at = get_copilot_token(
                    self._github_token
                )
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                body = _extract_error_detail(exc.response) if exc.response is not None else str(exc)
                raise CopilotAPIError(
                    f"Copilot token refresh failed (HTTP {status}). "
                    f"Your GitHub PAT may be expired or lack the 'copilot' scope.",
                    status_code=status if isinstance(status, int) else None,
                    endpoint="https://api.github.com/copilot_internal/v2/token",
                    response_body=body,
                ) from exc
            except Exception as exc:
                raise CopilotAPIError(
                    f"Copilot token refresh failed: {type(exc).__name__}: {exc}\n"
                    f"Check network connectivity and GitHub PAT validity.",
                ) from exc
            log.debug("[API] New Copilot token valid until %d",
                      self._token_expires_at)

    def _parse_sse(self, response: requests.Response) -> Generator[str, None, None]:
        """
        Yield text deltas from a server-sent-events (SSE) stream.
        Works for OpenAI / Gemini / GPT models.

        Each non-empty line has the form ``data: <json>``; ``data: [DONE]``
        signals the end of the stream.
        """
        chunk_count = 0
        error_count = 0
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
                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content", "")
                if delta:
                    chunk_count += 1
                    yield delta
            except json.JSONDecodeError:
                error_count += 1
                log.warning(
                    "[API] SSE: failed to parse JSON chunk (error #%d): %s",
                    error_count, payload[:200],
                )
            except (KeyError, IndexError) as exc:
                error_count += 1
                log.warning(
                    "[API] SSE: unexpected chunk structure (%s): %s",
                    exc, payload[:200],
                )
        if chunk_count == 0 and error_count == 0:
            log.warning(
                "[API] SSE: stream ended with 0 text chunks and 0 errors. "
                "The model may have returned an empty response.",
            )

    def _parse_claude_sse(self, response: requests.Response) -> Generator[str, None, None]:
        """
        Yield text deltas from a Claude (Anthropic Messages API) SSE stream.

        Claude emits events like::

            event: content_block_delta
            data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}

        The stream ends with ``event: message_stop``.
        """
        chunk_count = 0
        error_count = 0
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
                chunk_type = chunk.get("type", "")
                if chunk_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            chunk_count += 1
                            yield text
                elif chunk_type == "error":
                    err = chunk.get("error", {})
                    err_type = err.get("type", "unknown_error")
                    err_msg = err.get("message", str(err))
                    log.error(
                        "[API] Claude stream error [%s]: %s. "
                        "Full error chunk: %s",
                        err_type, err_msg, json.dumps(chunk)[:300],
                    )
                    # Yield the error as visible text so the user sees it
                    yield f"\n\n⚠️ Claude stream error [{err_type}]: {err_msg}"
                    break
            except json.JSONDecodeError:
                error_count += 1
                log.warning(
                    "[API] Claude SSE: failed to parse JSON chunk "
                    "(error #%d): %s",
                    error_count, payload[:200],
                )
            except (KeyError, IndexError) as exc:
                error_count += 1
                log.warning(
                    "[API] Claude SSE: unexpected chunk structure (%s): %s",
                    exc, payload[:200],
                )
        if chunk_count == 0 and error_count == 0:
            log.warning(
                "[API] Claude SSE: stream ended with 0 text chunks. "
                "The model may have returned an empty response or "
                "an unrecognised event format.",
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_model_limits(self) -> dict[str, 'ModelLimits']:
        """Fetch per-model token limits from the Copilot models endpoint.

        Populates ``self._model_limits`` and returns the mapping.
        On failure, logs a warning and returns whatever was previously cached.
        """
        self._ensure_token()
        headers = {
            **_COPILOT_HEADERS,
            "Authorization": f"Bearer {self._copilot_token}",
        }
        try:
            resp = requests.get(
                COPILOT_MODELS_URL, headers=headers, timeout=30,
            )
            log.debug(
                "[API] GET %s → %d  (body len=%d)",
                COPILOT_MODELS_URL,
                resp.status_code,
                len(resp.text),
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.HTTPError as exc:
            detail = _extract_error_detail(resp) if resp is not None else str(exc)
            log.warning(
                "[API] Failed to fetch model list: HTTP %d from %s — %s",
                resp.status_code if resp is not None else 0,
                COPILOT_MODELS_URL,
                detail,
            )
            return self._model_limits
        except requests.ConnectionError as exc:
            log.warning(
                "[API] Failed to fetch model list (network error): %s. "
                "Endpoint: %s",
                exc, COPILOT_MODELS_URL,
            )
            return self._model_limits
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[API] Failed to fetch model list: %s: %s. Endpoint: %s",
                type(exc).__name__, exc, COPILOT_MODELS_URL,
            )
            return self._model_limits

        # The endpoint may return {"data": [...]} or a bare list.
        if isinstance(body, dict) and "data" in body:
            models = body["data"]
        elif isinstance(body, list):
            models = body
        else:
            log.warning("[API] Unexpected models response format: %s",
                        type(body).__name__)
            return self._model_limits

        log.debug("[API] Models endpoint returned %d model entries.",
                  len(models))

        for entry in models:
            model_id: str = entry.get("id", "")
            caps = entry.get("capabilities", {})
            limits = caps.get("limits", {})

            log.debug(
                "[API]   model=%s  has_caps=%s  has_limits=%s  limit_keys=%s",
                model_id,
                bool(caps),
                bool(limits),
                list(limits.keys()) if limits else "(none)",
            )

            if not limits:
                continue

            lim = ModelLimits(
                max_context_window_tokens=limits.get(
                    "max_context_window_tokens", 8192,
                ),
                max_prompt_tokens=limits.get("max_prompt_tokens", 8192),
                max_output_tokens=limits.get("max_output_tokens", 1024),
            )
            # Store under the canonical ID …
            self._model_limits[model_id] = lim
            # … and normalised variants so look-ups always match.
            _store_model_aliases(self._model_limits, model_id, lim)

            log.info(
                "[API] Model %s limits — context: %d, prompt: %d, output: %d",
                model_id,
                lim.max_context_window_tokens,
                lim.max_prompt_tokens,
                lim.max_output_tokens,
            )

        log.debug("[API] Model limits cache keys: %s",
                  list(self._model_limits.keys()))
        return self._model_limits

    def get_model_limits(self, model_id: str) -> 'ModelLimits | None':
        """Return the cached :class:`ModelLimits` for *model_id*, or *None*."""
        return self._model_limits.get(model_id)

    def build_preview_payload(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
    ) -> dict:
        """Build the full API payload **without** sending it.

        Returns a dict containing ``"endpoint"``, ``"payload"``, and
        ``"message_count"`` so the caller can display a preview.
        """
        family = get_model_family(model)
        limits = self.get_model_limits(model)

        if family == "claude":
            formatted, system_text = _format_messages_claude(messages)
            max_out = limits.max_output_tokens if limits else 16_000
            payload = _build_payload_claude(
                formatted, model, stream=True,
                system_text=system_text,
                max_output_tokens=max_out,
            )
            endpoint = COPILOT_CLAUDE_URL
        elif family == "gemini":
            formatted = _format_messages_gemini(messages)
            payload = _build_payload_gemini(formatted, model, stream=True)
            endpoint = COPILOT_CHAT_URL
        else:
            formatted = _format_messages_openai(messages)
            payload = _build_payload_openai(formatted, model, stream=True)
            endpoint = COPILOT_CHAT_URL

        return {
            "endpoint": endpoint,
            "model_family": family,
            "message_count": len(formatted),
            "payload": payload,
        }

    def chat(
        self,
        messages: list[dict],
        model: str = "gpt-4o",
        stream: bool = True,
        *,
        pre_assembled: bool = False,
    ) -> Iterator[str] | str:
        """
        Send *messages* to the Copilot chat completions endpoint.

        Parameters
        ----------
        messages : OpenAI-style list of ``{"role": ..., "content": ...}`` dicts.
        model    : Copilot model identifier (see :data:`MODELS`).
        stream   : When *True* (default) returns a generator of text deltas;
                   when *False* returns the full response as a plain string.
        pre_assembled :
            When *True*, *messages* have already been trimmed to fit the
            model's prompt token budget (via
            :func:`~context_manager.build_messages_from_layout`).
            The internal ``build_context_window`` step is skipped.
        """
        self._ensure_token()

        family = get_model_family(model)

        # --- Debug: show what is actually being sent ---
        log.debug("[API] ── Sending chat request ──")
        log.debug("[API]   model = %s  |  family = %s  |  stream = %s",
                  model, family, stream)
        log.debug("[API]   message count = %d", len(messages))
        for i, m in enumerate(messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            preview = (content[:120] + "…") if isinstance(content, str) and len(content) > 120 else content
            if isinstance(content, list):
                preview = f"[multipart, {len(content)} parts]"
            log.debug("[API]   msg[%d] role=%-10s  content=%s",
                      i, role, preview)
        log.debug("[API] ─────────────────────────")

        # ---- Trim to fit context window ---------------------------------
        if pre_assembled:
            # Messages were already assembled and trimmed by the caller
            # (build_messages_from_layout); skip internal trimming.
            trimmed = messages
            limits = self.get_model_limits(model)
        else:
            limits = self.get_model_limits(model)
            if limits:
                log.debug(
                    "[API] Using dynamic limits for %s: prompt=%d, output=%d",
                    model, limits.max_prompt_tokens, limits.max_output_tokens,
                )
                trimmed = build_context_window(
                    messages,
                    max_tokens=limits.max_prompt_tokens,
                    reply_buffer_tokens=0,
                )
            else:
                log.debug(
                    "[API] No dynamic limits for model %s; using defaults.",
                    model,
                )
                trimmed = build_context_window(messages)

            if len(trimmed) < len(messages):
                log.info(
                    "[API] Message list trimmed from %d to %d messages "
                    "to fit context window.",
                    len(messages),
                    len(trimmed),
                )

        # ---- Model-specific formatting & payload ------------------------
        if family == "claude":
            formatted, system_text = _format_messages_claude(trimmed)
            max_out = limits.max_output_tokens if limits else 16_000
            payload = _build_payload_claude(
                formatted, model, stream,
                system_text=system_text,
                max_output_tokens=max_out,
            )
        elif family == "gemini":
            formatted = _format_messages_gemini(trimmed)
            payload = _build_payload_gemini(formatted, model, stream)
        else:  # openai / gpt
            formatted = _format_messages_openai(trimmed)
            payload = _build_payload_openai(formatted, model, stream)

        log.debug("[API] Payload keys: %s", list(payload.keys()))

        headers = {
            **_COPILOT_HEADERS,
            "Authorization": f"Bearer {self._copilot_token}",
        }

        # ---- Choose endpoint & send request -----------------------------
        if family == "claude":
            url = COPILOT_CLAUDE_URL
            log.debug("[API] Using Claude Messages endpoint: %s", url)
        else:
            url = COPILOT_CHAT_URL

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            stream=stream,
            timeout=120,
        )

        # ---- Handle HTTP errors with full diagnostic context ------------
        if not response.ok:
            detail = _extract_error_detail(response)
            raise CopilotAPIError(
                f"Copilot API request failed (HTTP {response.status_code}).\n"
                f"{detail}",
                status_code=response.status_code,
                endpoint=url,
                model=model,
                response_body=response.text[:500] if response.text else "",
                payload_summary=_summarise_payload(payload),
            )

        # ---- Parse response ---------------------------------------------
        if family == "claude":
            if stream:
                return self._parse_claude_sse(response)
            # Non-streaming Claude response
            try:
                body = response.json()
            except json.JSONDecodeError as exc:
                raise CopilotAPIError(
                    f"Claude returned non-JSON response (HTTP {response.status_code}).",
                    status_code=response.status_code,
                    endpoint=url,
                    model=model,
                    response_body=response.text[:500],
                ) from exc
            content_blocks = body.get("content", [])
            if not content_blocks:
                log.warning(
                    "[API] Claude response has no content blocks. "
                    "Full body: %s", json.dumps(body)[:500],
                )
            texts = [
                b.get("text", "")
                for b in content_blocks
                if b.get("type") == "text"
            ]
            return "\n".join(texts)

        if stream:
            return self._parse_sse(response)

        # Non-streaming OpenAI/Gemini response
        try:
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise CopilotAPIError(
                f"Unexpected response format from {family} model.\n"
                f"Expected 'choices[0].message.content' in response.",
                status_code=response.status_code,
                endpoint=url,
                model=model,
                response_body=response.text[:500],
            ) from exc


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _store_model_aliases(
    cache: dict[str, ModelLimits],
    model_id: str,
    lim: ModelLimits,
) -> None:
    """Store normalised aliases (dots ↔ hyphens, version variants)."""
    # dots ↔ hyphens
    for alt in (
        model_id.replace(".", "-"),
        model_id.replace("-", "."),
    ):
        if alt != model_id:
            cache[alt] = lim

    # Some Copilot model IDs use dot-separated versions (claude-opus-4.5)
    # while the MODELS dict uses hyphen-only (claude-opus-4-5).  Generate
    # all reasonable variants:
    #   claude-opus-4.5  →  claude-opus-4-5
    #   claude-opus-4-5  →  claude-opus-4.5
    # Also: gemini-1.5-pro  →  gemini-1-5-pro  (and vice-versa)
    parts = model_id.replace(".", "-").split("-")
    # Try re-joining with dots for each numeric segment boundary
    for i in range(1, len(parts)):
        if parts[i] and parts[i][0].isdigit():
            variant = "-".join(parts[:i]) + "." + ".".join(parts[i:])
            if variant != model_id and variant not in cache:
                cache[variant] = lim
