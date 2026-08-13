"""Focused Phase 1 tests; they use no real Codex process or network service."""

import asyncio
import importlib.util
import json
from unittest import mock
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("codex-api.py")
SPEC = importlib.util.spec_from_file_location("codex_api", MODULE_PATH)
assert SPEC and SPEC.loader
codex_api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_api
SPEC.loader.exec_module(codex_api)


def usage(input_tokens, cached_tokens, output_tokens, reasoning_tokens, **extra):
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        **extra,
    }


class UsageAccountingTests(unittest.TestCase):
    def test_first_turn_is_not_changed(self):
        totals = usage(100, 80, 20, 5)
        self.assertEqual(
            codex_api.responses_usage(codex_api.normalized_usage(totals)),
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 80},
                "output_tokens": 20,
                "output_tokens_details": {"reasoning_tokens": 5},
                "total_tokens": 120,
            },
        )

    def test_two_resumed_turns_include_cached_and_reasoning_deltas(self):
        first = usage(100, 80, 20, 5, cache_write_input_tokens=9)
        second = usage(180, 140, 35, 12, cache_write_input_tokens=15)
        third = usage(250, 200, 50, 18, cache_write_input_tokens=21)
        self.assertEqual(codex_api.incremental_usage(second, first), usage(80, 60, 15, 7, cache_write_input_tokens=6))
        self.assertEqual(codex_api.incremental_usage(third, second), usage(70, 60, 15, 6, cache_write_input_tokens=6))

    def test_regressing_or_malformed_totals_fall_back_without_negative_values(self):
        previous = usage(100, 80, 20, 5)
        regressing = usage(90, 70, 19, 4)
        self.assertEqual(codex_api.incremental_usage(regressing, previous), regressing)
        malformed = usage("bad", 70, 19, 4)
        returned = codex_api.incremental_usage(malformed, previous)
        self.assertEqual(returned, malformed)
        self.assertEqual(codex_api.normalized_usage(returned)["prompt_tokens"], 0)


class ResponseStateTests(unittest.TestCase):
    def test_legacy_version_one_record_loads_and_new_record_upgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 1, "responses": {"old": {"thread_id": "t1"}}}))
            state = codex_api.ResponseState(path)
            self.assertEqual(state.thread_for("old"), "t1")
            self.assertIsNone(state.cumulative_usage_for("old"))
            state.remember("new", "t1", usage(100, 80, 20, 5))
            persisted = json.loads(path.read_text())
            self.assertEqual(persisted["version"], 3)
            self.assertEqual(persisted["responses"]["old"]["thread_id"], "t1")
            self.assertEqual(persisted["responses"]["new"]["cumulative_usage"]["input_tokens"], 100)

    def test_version_two_state_loads_without_a_context_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 2, "responses": {
                "old": {"thread_id": "t1", "cumulative_usage": usage(10, 0, 2, 0)}
            }}))
            state = codex_api.ResponseState(path)
            self.assertIsNone(state.context_input_tokens_for("old"))
            state.remember("new", "t1", usage(20, 0, 4, 0), 12)
            self.assertEqual(json.loads(path.read_text())["responses"]["new"]["context_input_tokens"], 12)

    def test_failed_replace_preserves_old_file_and_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 1, "responses": {"old": {"thread_id": "t1"}}}))
            state = codex_api.ResponseState(path)
            before = path.read_text()
            with mock.patch.object(codex_api.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    state.remember("new", "t1", usage(100, 80, 20, 5))
            self.assertEqual(path.read_text(), before)
            self.assertNotIn("new", state.responses)


class SettingsTests(unittest.TestCase):
    def test_profile_instructions_is_resolved_relative_to_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instructions = root / "lean.md"
            instructions.write_text("Return text only.\n", encoding="utf-8")
            config = root / "config.yaml"
            config.write_text(
                "working_directory: .\nprofile_instructions: lean.md\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["codex-api.py", "--config", str(config)]):
                parsed = codex_api.parse_args()

            self.assertEqual(parsed.profile_instructions, instructions)

    def test_log_level_is_loaded_from_yaml_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instructions = root / "lean.md"
            instructions.write_text("Return text only.\n", encoding="utf-8")
            config = root / "config.yaml"
            config.write_text(
                "working_directory: .\nprofile_instructions: lean.md\nlog_level: WARNING\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["codex-api.py", "--config", str(config)]):
                parsed = codex_api.parse_args()

            self.assertEqual(parsed.log_level, "warning")

    def test_bearer_token_allow_list_is_loaded_from_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lean.md").write_text("Return text only.\n", encoding="utf-8")
            config = root / "config.yaml"
            config.write_text(
                "working_directory: .\nprofile_instructions: lean.md\nbearer_tokens: [first, second]\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["codex-api.py", "--config", str(config)]):
                parsed = codex_api.parse_args()

            self.assertEqual(parsed.bearer_tokens, frozenset({"first", "second"}))

    def test_bearer_token_allow_list_rejects_invalid_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lean.md").write_text("Return text only.\n", encoding="utf-8")
            config = root / "config.yaml"
            config.write_text(
                "working_directory: .\nprofile_instructions: lean.md\nbearer_tokens: invalid\n",
                encoding="utf-8",
            )
            with mock.patch.object(sys, "argv", ["codex-api.py", "--config", str(config)]):
                with self.assertRaises(SystemExit):
                    codex_api.parse_args()


class AuthenticationTests(unittest.TestCase):
    def test_standard_bearer_header_accepts_any_configured_token(self):
        scope = {"headers": [(b"authorization", b"Bearer second")]}
        self.assertTrue(codex_api.authorized(scope, frozenset({"first", "second"})))

    def test_missing_or_wrong_bearer_header_is_denied_when_tokens_configured(self):
        tokens = frozenset({"expected"})
        self.assertFalse(codex_api.authorized({"headers": []}, tokens))
        self.assertFalse(codex_api.authorized({"headers": [(b"authorization", b"Bearer wrong")]}, tokens))


class AuthenticationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_gate_protects_v1_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = codex_api.Settings(
                "127.0.0.1", 0, "codex", root, "read-only", 1,
                None, None, root / "instructions.md", None, 1, root / "state.json",
                bearer_tokens=frozenset({"expected"}),
            )
            app = codex_api.CodexApi(settings)
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await app(
                {"type": "http", "method": "GET", "path": "/v1/models", "headers": []},
                receive,
                send,
            )

            self.assertEqual(sent[0]["status"], 401)


class PromptRenderingTests(unittest.TestCase):
    def test_responses_plain_string_without_instructions_is_byte_exact(self):
        text = "  <user>delimiter-like</user>\n\tKeep every byte.  "
        self.assertEqual(codex_api.prompt_from_responses_request({"input": text}), text)

    def test_responses_plain_string_with_null_instructions_is_byte_exact(self):
        text = "\n\n"
        self.assertEqual(
            codex_api.prompt_from_responses_request({"input": text, "instructions": None}), text
        )

    def test_responses_plain_string_with_developer_instructions_keeps_wrapper(self):
        self.assertEqual(
            codex_api.prompt_from_responses_request(
                {"instructions": "Be terse.", "input": "hello"}
            ),
            "Respond to the input below. Return only the assistant's answer; do not "
            "describe this wrapper or the role labels.\n\n"
            "<developer>\nBe terse.\n</developer>\n\n<user>\nhello\n</user>",
        )

    def test_responses_empty_string_and_empty_instructions_retain_existing_wrapper(self):
        self.assertEqual(codex_api.prompt_from_responses_request({"input": ""}), "")
        self.assertEqual(
            codex_api.prompt_from_responses_request({}),
            "Respond to the input below. Return only the assistant's answer; do not "
            "describe this wrapper or the role labels.\n\n<user>\n\n</user>",
        )
        self.assertEqual(
            codex_api.prompt_from_responses_request({"instructions": "", "input": ""}),
            "Respond to the input below. Return only the assistant's answer; do not "
            "describe this wrapper or the role labels.\n\n<user>\n\n</user>",
        )

    def test_responses_message_arrays_keep_supported_roles_labelled(self):
        messages = [
            {"type": "message", "role": "system", "content": "system text"},
            {"type": "message", "role": "developer", "content": "developer text"},
            {"type": "message", "role": "user", "content": "user text"},
            {"type": "message", "role": "assistant", "content": "assistant text"},
        ]
        prompt = codex_api.prompt_from_responses_request({"input": messages})
        for role in ("system", "developer", "user", "assistant"):
            self.assertIn(f"<{role}>\n{role} text\n</{role}>", prompt)
        self.assertTrue(prompt.startswith("Respond to the input below."))

    def test_responses_rejects_unsupported_items_roles_and_instructions(self):
        with self.assertRaisesRegex(ValueError, "type is unsupported"):
            codex_api.prompt_from_responses_request({"input": [{"type": "image"}]})
        with self.assertRaisesRegex(ValueError, "role is unsupported"):
            codex_api.prompt_from_responses_request({"input": [{"role": "tool", "content": "x"}]})
        with self.assertRaisesRegex(ValueError, "instructions.*string or null"):
            codex_api.prompt_from_responses_request({"input": "x", "instructions": 1})

    def test_chat_completions_rendering_is_unchanged(self):
        self.assertEqual(
            codex_api.prompt_from_messages([{"role": "user", "content": "hello"}]),
            "Respond to the conversation below. Return only the assistant's answer "
            "to the final user message; do not describe this wrapper or the role labels.\n\n"
            "<user>\nhello\n</user>",
        )


class CompactionValidationTests(unittest.TestCase):
    def test_documented_context_management_form(self):
        self.assertEqual(codex_api.compaction_threshold([
            {"type": "compaction", "compact_threshold": 123}
        ]), 123)
        self.assertIsNone(codex_api.compaction_threshold(None))

    def test_rejects_invalid_context_management(self):
        invalid = [
            {}, [], [{}], [{"type": "other", "compact_threshold": 1}],
            [{"type": "compaction", "compact_threshold": 0}],
            [{"type": "compaction", "compact_threshold": True}],
            [{"type": "compaction", "compact_threshold": 1, "extra": 1}],
            [{"type": "compaction", "compact_threshold": 1}] * 2,
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                codex_api.compaction_threshold(value)


class ResponsesStoreFalseTests(unittest.IsolatedAsyncioTestCase):
    async def test_unstored_continuation_uses_predecessor_delta(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = codex_api.Settings(
                "127.0.0.1", 0, "codex", Path(directory), "read-only", 1,
                None, None, Path(directory) / "instructions.md", None, 1,
                Path(directory) / "state.json",
            )
            app = codex_api.CodexApi(settings)
            app.response_state.remember("previous", "thread-1", usage(100, 80, 10, 2))

            async def fake_run(*args, **kwargs):
                self.assertEqual(kwargs["thread_id"], "thread-1")
                self.assertTrue(kwargs["ephemeral"])
                return codex_api.TurnResult(
                    "answer",
                    usage(60, 50, 15, 5),
                    usage(160, 130, 25, 7),
                    "thread-1",
                )

            app.run_codex = fake_run
            sent = []

            async def receive():
                return {"type": "http.request", "body": json.dumps({
                    "input": "next", "previous_response_id": "previous", "store": False
                }).encode(), "more_body": False}

            async def send(message):
                sent.append(message)

            await app.handle_responses(receive, send)
            body = json.loads(sent[-1]["body"])
            self.assertEqual(body["usage"]["input_tokens"], 60)
            self.assertEqual(body["usage"]["input_tokens_details"]["cached_tokens"], 50)
            self.assertEqual(body["usage"]["output_tokens"], 15)
            self.assertEqual(body["usage"]["output_tokens_details"]["reasoning_tokens"], 5)
            self.assertFalse(body["store"])
            self.assertEqual(len(app.response_state.responses), 1)


if __name__ == "__main__":
    unittest.main()
