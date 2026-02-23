"""Tests for src/context_manager.py.

These tests use the approximate (non-tiktoken) counting path so that they
run without network access.  The tiktoken path is exercised only when the
BPE file is available in the cache.
"""

import unittest

import src.context_manager as cm


class TestCountMessageTokens(unittest.TestCase):

    def test_simple_string_message(self) -> None:
        msg = {"role": "user", "content": "Hello"}
        tokens = cm.count_message_tokens(msg)
        # Must be >= TOKENS_PER_MESSAGE
        self.assertGreaterEqual(tokens, cm.TOKENS_PER_MESSAGE)

    def test_empty_content(self) -> None:
        msg = {"role": "user", "content": ""}
        tokens = cm.count_message_tokens(msg)
        # Empty string → max(1, (0+2)//3) = 1 approximate token
        self.assertEqual(tokens, cm.TOKENS_PER_MESSAGE + 1)

    def test_multipart_content(self) -> None:
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }
        tokens = cm.count_message_tokens(msg)
        # Must account for image cost (256) + text + framing
        self.assertGreaterEqual(tokens, cm.TOKENS_PER_MESSAGE + 256)

    def test_count_messages_tokens_includes_priming(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        total = cm.count_messages_tokens(messages)
        individual = sum(cm.count_message_tokens(m) for m in messages) + 3
        self.assertEqual(total, individual)


class TestBuildContextWindow(unittest.TestCase):

    def _make_history(self, n_pairs: int) -> list[dict]:
        """Return a flat list of n_pairs user/assistant message dicts."""
        msgs = []
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"Question {i}"})
            msgs.append({"role": "assistant", "content": f"Answer {i}"})
        return msgs

    def test_empty_messages_returns_empty(self) -> None:
        self.assertEqual(cm.build_context_window([]), [])

    def test_single_user_message_fits(self) -> None:
        msgs = [{"role": "user", "content": "Hello"}]
        result = cm.build_context_window(msgs, max_tokens=8192)
        self.assertEqual(result, msgs)

    def test_system_message_always_at_front(self) -> None:
        msgs = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        result = cm.build_context_window(msgs, max_tokens=8192)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[-1]["role"], "user")

    def test_all_messages_fit_unchanged(self) -> None:
        history = self._make_history(3)
        history.append({"role": "user", "content": "last"})
        result = cm.build_context_window(history, max_tokens=8192)
        self.assertEqual(result, history)

    def test_history_trimmed_to_fit_budget(self) -> None:
        history = self._make_history(50)
        history.append({"role": "user", "content": "Final question"})

        # Budget must exceed REPLY_BUFFER_TOKENS but be small enough to
        # force trimming of the 50-pair history (~900 tokens).
        result = cm.build_context_window(history, max_tokens=1500)
        # Result must be shorter
        self.assertLess(len(result), len(history))
        # Current user message is always last
        self.assertEqual(result[-1]["content"], "Final question")
        # Total tokens within budget
        used = cm.count_messages_tokens(result)
        self.assertLessEqual(used, 1500 - cm.REPLY_BUFFER_TOKENS)

    def test_system_always_included_when_history_trimmed(self) -> None:
        history = [{"role": "system", "content": "You are helpful."}]
        history += self._make_history(20)
        history.append({"role": "user", "content": "New question"})

        result = cm.build_context_window(history, max_tokens=1500)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[-1]["content"], "New question")

    def test_oversized_user_message_raises_value_error(self) -> None:
        big_msg = "x" * 10_000
        msgs = [{"role": "user", "content": big_msg}]
        with self.assertRaises(ValueError) as ctx:
            cm.build_context_window(msgs, max_tokens=100)
        # Error message should mention tokens (in Korean)
        self.assertIn("토큰", str(ctx.exception))

    def test_oversized_system_plus_user_raises(self) -> None:
        msgs = [
            {"role": "system", "content": "s" * 5_000},
            {"role": "user", "content": "u" * 5_000},
        ]
        with self.assertRaises(ValueError):
            cm.build_context_window(msgs, max_tokens=500)

    def test_reply_buffer_tokens_zero(self) -> None:
        """When reply_buffer_tokens=0 the full budget is usable."""
        msgs = [{"role": "user", "content": "Hello"}]
        result = cm.build_context_window(
            msgs, max_tokens=8192, reply_buffer_tokens=0,
        )
        self.assertEqual(result, msgs)
        used = cm.count_messages_tokens(result)
        # With reply_buffer=0, effective budget == max_tokens
        self.assertLessEqual(used, 8192)

    def test_custom_reply_buffer(self) -> None:
        """A large reply_buffer_tokens shrinks the effective budget."""
        big_msg = "x" * 10_000
        msgs = [{"role": "user", "content": big_msg}]
        # With reply_buffer=0 and a generous max_tokens, this should fit.
        result = cm.build_context_window(
            msgs, max_tokens=128_000, reply_buffer_tokens=0,
        )
        self.assertEqual(len(result), 1)
        # But with a huge reply buffer it should fail.
        with self.assertRaises(ValueError):
            cm.build_context_window(
                msgs, max_tokens=128_000, reply_buffer_tokens=128_000,
            )

    def test_recent_history_kept_older_dropped(self) -> None:
        sys_msg = {"role": "system", "content": "Be brief."}
        old_pair = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ]
        new_pair = [
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
        ]
        current = {"role": "user", "content": "current"}

        # Calculate a budget that fits sys + new_pair + current exactly,
        # but not old_pair as well.
        enough_for_new_only = (
            cm.count_messages_tokens([sys_msg] + new_pair + [current])
            + cm.REPLY_BUFFER_TOKENS
        )
        messages = [sys_msg] + old_pair + new_pair + [current]
        result = cm.build_context_window(messages, max_tokens=enough_for_new_only)

        contents = [m["content"] for m in result]
        self.assertIn("recent question", contents)
        self.assertIn("recent answer", contents)
        self.assertNotIn("old question", contents)
        self.assertNotIn("old answer", contents)

    def test_result_always_within_token_budget(self) -> None:
        history = self._make_history(100)
        history.append({"role": "user", "content": "tell me everything"})
        max_tokens = 2000
        result = cm.build_context_window(history, max_tokens=max_tokens)
        used = cm.count_messages_tokens(result)
        self.assertLessEqual(used, max_tokens - cm.REPLY_BUFFER_TOKENS)

    def test_current_user_message_always_last(self) -> None:
        history = self._make_history(10)
        history.append({"role": "user", "content": "the question"})
        for budget in [1200, 2000, 8192]:
            result = cm.build_context_window(history, max_tokens=budget)
            self.assertEqual(result[-1]["content"], "the question",
                             f"Last message wrong with budget={budget}")


if __name__ == "__main__":
    unittest.main()
