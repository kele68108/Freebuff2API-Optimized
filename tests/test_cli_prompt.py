import tempfile
import unittest
from pathlib import Path

from freebuff2api.cli_prompt import extract_cli_system_prompt
from freebuff2api.codebuff import CHAT_COMPLETIONS_USER_AGENT, FreebuffSession
from freebuff2api.openai_compat import build_upstream_payload, normalize_chat_messages

MARKER = b"You are Buffy, the strategic coding assistant"
END_MARK = b"See freebuff.com for more information about the product."


def _synthetic_cli(tmp: str) -> str:
    """Build a fake CLI binary containing a real-length Buffy prompt."""
    newline = bytes((10,))  # real newline byte (no escape sequence in source)
    prompt = (
        MARKER
        + newline
        + newline.join([b"line of prompt filler " + str(i).encode() for i in range(200)])
        + newline
        + END_MARK
    )
    assert 800 <= len(prompt) <= 12000, len(prompt)
    payload = b"bun-bundle-prefix-" * 2000 + prompt + b"-trailing-garbage" * 500
    path = Path(tmp) / "freebuff"
    path.write_bytes(payload)
    return str(path)


class CliPromptExtractionTests(unittest.TestCase):
    def test_extracts_prompt_from_synthetic_cli_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = _synthetic_cli(tmp)
            prompt = extract_cli_system_prompt(cli_path)
            self.assertIn("You are Buffy, the strategic coding assistant", prompt)
            self.assertIn("See freebuff.com for more information about the product.", prompt)
            self.assertGreater(len(prompt), 800)

    def test_missing_binary_returns_empty(self) -> None:
        self.assertEqual(extract_cli_system_prompt("/nonexistent/freebuff"), "")

    def test_none_path_returns_empty(self) -> None:
        self.assertEqual(extract_cli_system_prompt(None), "")

    def test_garbage_binary_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "freebuff"
            path.write_bytes(b"no marker here at all" * 100)
            self.assertEqual(extract_cli_system_prompt(str(path)), "")


class GateCompliantNormalizationTests(unittest.TestCase):
    def test_normalize_prepends_real_cli_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = _synthetic_cli(tmp)
            messages = normalize_chat_messages(
                [{"role": "user", "content": "hello"}],
                cli_path=cli_path,
            )
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn(
                "You are Buffy, the strategic coding assistant",
                messages[0]["content"],
            )
            self.assertEqual(messages[1]["role"], "user")

    def test_normalize_does_not_double_inject_when_client_sent_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = _synthetic_cli(tmp)
            messages = normalize_chat_messages(
                [
                    {
                        "role": "system",
                        "content": "You are Buffy, the strategic coding assistant. client body",
                    },
                    {"role": "user", "content": "hello"},
                ],
                cli_path=cli_path,
            )
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[0]["content"], "You are Buffy, the strategic coding assistant. client body")
            self.assertEqual(len(messages), 2)

    def test_normalize_without_cli_binary_falls_back_to_minimal_system(self) -> None:
        messages = normalize_chat_messages(
            [{"role": "user", "content": "hello"}],
            cli_path="",
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Buffy", messages[0]["content"])

    def test_build_upstream_payload_threads_cli_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = _synthetic_cli(tmp)
            payload = build_upstream_payload(
                {"model": "deepseek/deepseek-v4-pro", "messages": [{"role": "user", "content": "hi"}]},
                session=FreebuffSession(instance_id="instance-1", model="deepseek/deepseek-v4-pro"),
                run_id="run-1",
                client_id="client-1",
                trace_session_id="trace-1",
                cli_path=cli_path,
            )
            first = payload["messages"][0]
            self.assertEqual(first["role"], "system")
            self.assertIn("You are Buffy, the strategic coding assistant", first["content"])

    def test_chat_completions_user_agent_matches_freebuff_go_gate_signature(self) -> None:
        # The upstream free_mode_cli_required gate fingerprints on this exact
        # user-agent. freebuff-go/client.go cliUserAgent must match byte-for-byte.
        self.assertEqual(
            CHAT_COMPLETIONS_USER_AGENT,
            "ai-sdk/openai-compatible/0.0.0-test/codebuff "
            "ai-sdk/provider-utils/3.0.25 runtime/browser",
        )

    def test_normalize_with_cli_prompt_marks_ephemeral_cache_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = _synthetic_cli(tmp)
            messages = normalize_chat_messages(
                [{"role": "user", "content": "hello"}],
                cli_path=cli_path,
            )
            self.assertEqual(messages[0]["cache_control"], {"type": "ephemeral"})


if __name__ == "__main__":
    unittest.main()
