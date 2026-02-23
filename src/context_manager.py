"""Token-aware context window manager.

Uses the ``o200k_base`` tokeniser (tiktoken) to count tokens and trim the
message list so that it fits within a configurable token budget.

Strategy
--------
1. The **system message** (if present at index 0) is always included.
2. The **current user message** (last element) is always included.
3. If system + current user query alone exceed the budget → :exc:`ValueError`
   is raised so the caller can inform the user to shorten the message.
4. Any remaining budget is filled with **as much recent history as possible**,
   starting from the most-recent exchange and working backwards.  Older
   messages are dropped first.

Token counting
--------------
* tiktoken with the ``o200k_base`` encoding is used when available.
* If tiktoken cannot load the BPE file (e.g. no internet access on first run),
  a character-based approximation (1 token ≈ 3 characters) is used instead
  and a warning is emitted.
"""

import logging

log = logging.getLogger("copilot_chatbot")

# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------

#: Default maximum context window in tokens.  Conservative value that works
#: across all Copilot-supported models while leaving head-room for the reply.
MAX_CONTEXT_TOKENS: int = 8_192

#: Tokens reserved for the model's reply.
REPLY_BUFFER_TOKENS: int = 1_024

#: Per-message framing overhead charged by the OpenAI message format.
TOKENS_PER_MESSAGE: int = 4

# ---------------------------------------------------------------------------
# Tokeniser bootstrap
# ---------------------------------------------------------------------------

try:
    import tiktoken  # type: ignore

    _enc = tiktoken.get_encoding("o200k_base")
    _TIKTOKEN_AVAILABLE = True
    log.debug("[CTX] tiktoken o200k_base encoder loaded successfully.")
except Exception:  # noqa: BLE001 – covers ImportError and network errors
    _enc = None  # type: ignore
    _TIKTOKEN_AVAILABLE = False
    log.warning(
        "[CTX] tiktoken o200k_base encoder is unavailable. "
        "Falling back to approximate token counting (1 token ≈ 3 chars). "
        "Install tiktoken and ensure network access on first run for accurate counts."
    )


# ---------------------------------------------------------------------------
# Token counting helpers
# ---------------------------------------------------------------------------

def _count_str_tokens(text: str) -> int:
    """Return the token count for a plain string."""
    if _TIKTOKEN_AVAILABLE and _enc is not None:
        return len(_enc.encode(text))
    # Approximation: 3 characters per token (conservative for Korean/mixed text)
    return max(1, (len(text) + 2) // 3)


def count_message_tokens(message: dict) -> int:
    """Return the token count for a single OpenAI-style message dict.

    The count includes the per-message framing overhead defined by
    :data:`TOKENS_PER_MESSAGE`.
    """
    tokens = TOKENS_PER_MESSAGE
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += _count_str_tokens(content)
    elif isinstance(content, list):
        # Multipart content (text blocks + images)
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                tokens += _count_str_tokens(part.get("text", ""))
            elif part.get("type") == "image_url":
                # Fixed-cost approximation for an image tile
                tokens += 256
    return tokens


def count_messages_tokens(messages: list[dict]) -> int:
    """Return the total token count for a list of messages.

    Adds the 3-token reply-priming overhead that the OpenAI API charges.
    """
    return sum(count_message_tokens(m) for m in messages) + 3


# ---------------------------------------------------------------------------
# Context window builder
# ---------------------------------------------------------------------------

def build_context_window(
    messages: list[dict],
    max_tokens: int = MAX_CONTEXT_TOKENS,
    reply_buffer_tokens: int = REPLY_BUFFER_TOKENS,
) -> list[dict]:
    """Return a trimmed copy of *messages* that fits within *max_tokens*.

    Parameters
    ----------
    messages:
        Full OpenAI-style message list.  May start with a ``"system"``
        message and **must** end with the current ``"user"`` message.
    max_tokens:
        Token budget for the entire request (defaults to
        :data:`MAX_CONTEXT_TOKENS`).  When the caller has dynamic
        per-model limits (e.g. ``max_prompt_tokens`` from the API),
        pass them here.
    reply_buffer_tokens:
        Tokens reserved for the model's reply (defaults to
        :data:`REPLY_BUFFER_TOKENS`).  Set to ``0`` when *max_tokens*
        already represents the pure prompt budget (i.e. the API's
        ``max_prompt_tokens`` which excludes output tokens).

    Returns
    -------
    list[dict]
        Trimmed message list guaranteed to fit within
        ``max_tokens - reply_buffer_tokens``.

    Raises
    ------
    ValueError
        When the system prompt + current user message alone exceed the
        available budget.  The caller should display the error string to the
        user and ask them to shorten their message or remove attachments.
    """
    if not messages:
        return []

    effective_budget = max_tokens - reply_buffer_tokens

    # ---- Separate parts -------------------------------------------------
    system_msgs: list[dict] = []
    history_msgs: list[dict] = []
    current_user_msg = messages[-1]  # always the last element

    for msg in messages[:-1]:
        if msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            history_msgs.append(msg)

    # ---- Validate mandatory parts ---------------------------------------
    fixed_msgs = system_msgs + [current_user_msg]
    fixed_tokens = count_messages_tokens(fixed_msgs)

    if fixed_tokens > effective_budget:
        raise ValueError(
            f"현재 메시지가 너무 큽니다 ({fixed_tokens:,} 토큰). "
            f"허용 가능한 최대 크기는 {effective_budget:,} 토큰입니다 "
            f"(시스템 프롬프트 포함, 응답 공간 {reply_buffer_tokens:,} 토큰 제외). "
            f"메시지를 줄이거나 첨부 파일을 제거해 주세요.\n\n"
            f"Your message is too large ({fixed_tokens:,} tokens). "
            f"Maximum allowed is {effective_budget:,} tokens "
            f"(including system prompt, excluding {reply_buffer_tokens:,} reply tokens). "
            f"Please shorten your message or remove attachments."
        )

    # ---- Greedily fill history -----------------------------------------
    remaining = effective_budget - fixed_tokens
    selected_history: list[dict] = []

    # Walk from most-recent to oldest; stop when budget runs out so that
    # the user/assistant turn alternation is preserved.
    for msg in reversed(history_msgs):
        t = count_message_tokens(msg)
        if t <= remaining:
            selected_history.insert(0, msg)
            remaining -= t
        else:
            # Dropping a message in the middle breaks the alternation, so
            # we stop here and discard all older messages as well.
            break

    if len(selected_history) < len(history_msgs):
        dropped = len(history_msgs) - len(selected_history)
        log.info(
            "[CTX] Context window trimmed: dropped %d oldest message(s) "
            "to stay within %d-token limit.",
            dropped,
            max_tokens,
        )

    result = system_msgs + selected_history + [current_user_msg]
    log.debug(
        "[CTX] build_context_window: %d → %d messages  "
        "(budget %d, used ~%d tokens)",
        len(messages),
        len(result),
        max_tokens,
        count_messages_tokens(result),
    )
    return result
