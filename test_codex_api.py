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
            self.assertEqual(persisted["version"], 2)
            self.assertEqual(persisted["responses"]["old"]["thread_id"], "t1")
            self.assertEqual(persisted["responses"]["new"]["cumulative_usage"]["input_tokens"], 100)

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
