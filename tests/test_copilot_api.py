"""Tests for model-specific message formatters and payload builders.

These tests cover the pure-function helpers in ``src.copilot_api`` and do
NOT make real HTTP requests.
"""

import unittest

from src.copilot_api import (
    ModelLimits,
    _build_payload_claude,
    _build_payload_gemini,
    _build_payload_openai,
    _convert_image_parts_for_claude,
    _flatten_content,
    _format_messages_claude,
    _format_messages_gemini,
    _format_messages_openai,
    _merge_messages,
    _store_model_aliases,
    get_model_family,
)


# -----------------------------------------------------------------------
# Model family detection
# -----------------------------------------------------------------------

class TestGetModelFamily(unittest.TestCase):

    def test_claude(self) -> None:
        self.assertEqual(get_model_family("claude-opus-4-5"), "claude")
        self.assertEqual(get_model_family("claude-opus-4.5"), "claude")

    def test_gemini(self) -> None:
        self.assertEqual(get_model_family("gemini-3-pro-preview"), "gemini")
        self.assertEqual(get_model_family("gemini-2.0-flash"), "gemini")

    def test_openai(self) -> None:
        self.assertEqual(get_model_family("gpt-4.1"), "openai")
        self.assertEqual(get_model_family("gpt-4o"), "openai")

    def test_unknown_defaults_to_openai(self) -> None:
        self.assertEqual(get_model_family("some-new-model"), "openai")


# -----------------------------------------------------------------------
# OpenAI formatter / payload
# -----------------------------------------------------------------------

class TestOpenAIFormatter(unittest.TestCase):

    def test_passthrough(self) -> None:
        msgs = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        self.assertEqual(_format_messages_openai(msgs), msgs)

    def test_payload_includes_n_and_top_p(self) -> None:
        payload = _build_payload_openai(
            [{"role": "user", "content": "Hi"}], "gpt-4o", True,
        )
        self.assertIn("n", payload)
        self.assertIn("top_p", payload)
        self.assertEqual(payload["model"], "gpt-4o")
        self.assertTrue(payload["stream"])


# -----------------------------------------------------------------------
# Gemini formatter / payload
# -----------------------------------------------------------------------

class TestGeminiFormatter(unittest.TestCase):

    def test_text_passthrough(self) -> None:
        msgs = [{"role": "user", "content": "Hello"}]
        self.assertEqual(_format_messages_gemini(msgs), msgs)

    def test_image_replaced_with_placeholder(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            },
        ]
        result = _format_messages_gemini(msgs)
        parts = result[0]["content"]
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[0]["text"], "Describe this")
        self.assertEqual(parts[1]["type"], "text")
        self.assertIn("does not support", parts[1]["text"])

    def test_payload_includes_n(self) -> None:
        payload = _build_payload_gemini(
            [{"role": "user", "content": "Hi"}], "gemini-1.5-pro", True,
        )
        self.assertIn("n", payload)


# -----------------------------------------------------------------------
# Claude formatter / payload
# -----------------------------------------------------------------------

class TestClaudeFormatter(unittest.TestCase):

    def test_system_extracted(self) -> None:
        msgs = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        body, sys_text = _format_messages_claude(msgs)
        self.assertEqual(sys_text, "Be helpful.")
        # System should NOT appear in the body
        roles = [m["role"] for m in body]
        self.assertNotIn("system", roles)
        self.assertEqual(body[-1]["content"], "Hello")

    def test_consecutive_same_role_merged(self) -> None:
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How are you?"},
        ]
        body, _ = _format_messages_claude(msgs)
        self.assertEqual(len(body), 1)
        self.assertIn("Hello", body[0]["content"])
        self.assertIn("How are you?", body[0]["content"])

    def test_alternation_preserved(self) -> None:
        msgs = [
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
        ]
        body, _ = _format_messages_claude(msgs)
        roles = [m["role"] for m in body]
        self.assertEqual(roles, ["user", "assistant", "user"])

    def test_starts_with_user(self) -> None:
        msgs = [
            {"role": "assistant", "content": "I'm ready."},
            {"role": "user", "content": "Go"},
        ]
        body, _ = _format_messages_claude(msgs)
        self.assertEqual(body[0]["role"], "user")

    def test_payload_no_n_no_top_p(self) -> None:
        payload = _build_payload_claude(
            [{"role": "user", "content": "Hi"}],
            "claude-opus-4-5",
            True,
            system_text="Be helpful.",
        )
        self.assertNotIn("n", payload)
        self.assertNotIn("top_p", payload)
        self.assertEqual(payload["system"], "Be helpful.")
        self.assertEqual(payload["model"], "claude-opus-4-5")
        # max_tokens is required for Claude Messages API
        self.assertIn("max_tokens", payload)
        self.assertGreater(payload["max_tokens"], 0)

    def test_payload_custom_max_output_tokens(self) -> None:
        payload = _build_payload_claude(
            [{"role": "user", "content": "Hi"}],
            "claude-opus-4-5",
            True,
            max_output_tokens=32_000,
        )
        self.assertEqual(payload["max_tokens"], 32_000)

    def test_payload_no_system_key_when_none(self) -> None:
        payload = _build_payload_claude(
            [{"role": "user", "content": "Hi"}],
            "claude-opus-4-5",
            True,
        )
        self.assertNotIn("system", payload)

    def test_image_parts_converted(self) -> None:
        parts = [
            {"type": "text", "text": "Describe"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
            },
        ]
        converted = _convert_image_parts_for_claude(parts)
        self.assertEqual(converted[0]["type"], "text")
        self.assertEqual(converted[1]["type"], "image")
        src = converted[1]["source"]
        self.assertEqual(src["type"], "base64")
        self.assertEqual(src["media_type"], "image/png")
        self.assertEqual(src["data"], "iVBORw0KGgo=")


# -----------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_flatten_content_string(self) -> None:
        self.assertEqual(_flatten_content("hello"), "hello")

    def test_flatten_content_list(self) -> None:
        parts = [
            {"type": "text", "text": "A"},
            {"type": "image_url"},
            {"type": "text", "text": "B"},
        ]
        result = _flatten_content(parts)
        self.assertIn("A", result)
        self.assertIn("B", result)
        self.assertIn("[image]", result)

    def test_merge_text_messages(self) -> None:
        a = {"role": "user", "content": "Hello"}
        b = {"role": "user", "content": "World"}
        merged = _merge_messages(a, b)
        self.assertEqual(merged["role"], "user")
        self.assertIn("Hello", merged["content"])
        self.assertIn("World", merged["content"])

    def test_store_model_aliases(self) -> None:
        cache: dict[str, ModelLimits] = {}
        lim = ModelLimits(160000, 128000, 32000)
        cache["claude-opus-4.5"] = lim
        _store_model_aliases(cache, "claude-opus-4.5", lim)
        # Must include hyphen-only variant
        self.assertIn("claude-opus-4-5", cache)
        self.assertIs(cache["claude-opus-4-5"], lim)


if __name__ == "__main__":
    unittest.main()
