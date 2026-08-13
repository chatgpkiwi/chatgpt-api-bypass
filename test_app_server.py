"""Phase 5 transport tests using the deterministic local fake App Server."""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("codex-api.py")
SPEC = importlib.util.spec_from_file_location("codex_api_app_tests", MODULE_PATH)
assert SPEC and SPEC.loader
codex_api = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = codex_api
SPEC.loader.exec_module(codex_api)

FAKE = Path(__file__).with_name("fake_app_server.py")


def settings(directory: Path, *, timeout: float = 1) -> codex_api.Settings:
    instructions = directory / "instructions.md"
    instructions.write_text("Return only text.\n", encoding="utf-8")
    return codex_api.Settings(
        "127.0.0.1", 0, str(FAKE), directory, "read-only", timeout,
        "gpt-5.6-luna", "low", instructions, None, 2,
        directory / "state.json", 1,
    )


async def request(app, path, payload):
    sent = []

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)
    return sent[0]["status"], json.loads(sent[-1]["body"])


class AppServerTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.codex_home = tempfile.TemporaryDirectory()
        self.codex_home_patch = mock.patch.dict(
            codex_api.os.environ, {"CODEX_HOME": self.codex_home.name}
        )
        self.codex_home_patch.start()

    def tearDown(self):
        self.codex_home_patch.stop()
        self.codex_home.cleanup()

    def test_named_profile_becomes_process_local_config_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory))
            codex_api._ensure_lean_profile(config.profile_instructions)
            client = codex_api.AppServerClient(config)
            command = client._command()
            self.assertNotIn("--profile", command)
            self.assertTrue(any(item.startswith("model_catalog_json=") for item in command))
            self.assertIn("features.shell_tool=false", command)

    async def test_process_disables_every_inherited_mcp_name_before_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            request_log = root / "requests.jsonl"
            base_config = Path(self.codex_home.name) / "config.toml"
            base_config.write_text(
                '''[mcp_servers.enabled]
url = "https://example.invalid/enabled"

[mcp_servers."dot.name / spaces"]
command = "never-run"

[mcp_servers.already_disabled]
url = "https://example.invalid/disabled"
enabled = false
required = true
''',
                encoding="utf-8",
            )
            project_config = root / ".codex" / "config.toml"
            project_config.parent.mkdir()
            project_config.write_text(
                '''[mcp_servers.future_server]
command = "never-run"
''',
                encoding="utf-8",
            )
            with mock.patch.dict(codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}):
                client = codex_api.AppServerClient(config)
                await client.start()
                try:
                    self.assertTrue(client.healthy)
                finally:
                    await client.stop()

            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            argv = next(record["params"] for record in records if record["method"] == "process/argv")
            overrides = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--config"]
            self.assertIn(
                'mcp_servers={already_disabled={enabled=false},"dot.name / spaces"={enabled=false},enabled={enabled=false},future_server={enabled=false}}',
                overrides,
            )
            status = next(record["params"] for record in records if record["method"] == "mcpServerStatus/list")
            self.assertEqual(status, {"detail": "toolsAndAuthOnly"})

    async def test_mcp_inventory_nonempty_malformed_or_paginated_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory))
            for mode, expected in (
                ("one", "still exposes an MCP server"),
                ("several", "still exposes an MCP server"),
                ("malformed", "malformed mcpServerStatus/list"),
                ("paged", "paginated MCP inventory"),
            ):
                with self.subTest(mode=mode), mock.patch.dict(
                    codex_api.os.environ, {"FAKE_APP_SERVER_MCP_STATUS": mode}
                ):
                    client = codex_api.AppServerClient(config)
                    with self.assertRaisesRegex(codex_api.AppServerUnavailable, expected):
                        await client.start()
                    self.assertFalse(client.healthy)

    async def test_malformed_mcp_config_fails_before_app_server_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (Path(self.codex_home.name) / "config.toml").write_text(
                "mcp_servers = []\n", encoding="utf-8"
            )
            client = codex_api.AppServerClient(settings(root))
            with self.assertRaisesRegex(codex_api.AppServerUnavailable, "invalid mcp_servers table"):
                await client.start()
            self.assertIsNone(client.process)

    async def test_restart_reloads_newly_added_mcp_server_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            base_config = Path(self.codex_home.name) / "config.toml"
            base_config.write_text('[mcp_servers.before]\ncommand = "never-run"\n', encoding="utf-8")
            with mock.patch.dict(codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}):
                first = codex_api.AppServerClient(settings(root))
                await first.start()
                await first.stop()
                base_config.write_text(
                    '''[mcp_servers.before]
command = "never-run"

[mcp_servers.after_upgrade]
command = "never-run"
''',
                    encoding="utf-8",
                )
                second = codex_api.AppServerClient(settings(root))
                await second.start()
                await second.stop()

            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            argv_records = [record["params"] for record in records if record["method"] == "process/argv"]
            self.assertEqual(len(argv_records), 2)
            self.assertFalse(any("after_upgrade={enabled=false}" in value for value in argv_records[0]))
            self.assertTrue(any("after_upgrade={enabled=false}" in value for value in argv_records[1]))

    async def test_startup_repairs_a_missing_or_mispointed_fixed_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory))
            profile_path = Path(self.codex_home.name) / "codex-api-lean.config.toml"
            profile_path.parent.mkdir(exist_ok=True)
            profile_path.write_text('model_instructions_file = "wrong.md"\n', encoding="utf-8")

            client = codex_api.AppServerClient(config)
            await client.start()
            self.addAsyncCleanup(client.stop)

            with profile_path.open("rb") as profile_file:
                profile = codex_api.tomllib.load(profile_file)
            self.assertEqual(profile["model_instructions_file"], str(config.profile_instructions))
            self.assertFalse(profile["features"]["shell_tool"])
            self.assertNotIn("mcp_servers", profile)

    async def test_handshake_new_thread_text_and_incremental_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            client = codex_api.AppServerClient(settings(Path(directory)))
            await client.start()
            try:
                self.assertTrue(client.healthy)
                result = await client.run_turn("hello", ephemeral=False)
                self.assertEqual(result.text, "answer:hello")
                self.assertEqual(result.usage, {
                    "input_tokens": 10, "cached_input_tokens": 4,
                    "output_tokens": 3, "reasoning_output_tokens": 1,
                })
                self.assertEqual(result.cumulative_usage, result.usage)
            finally:
                process = client.process
                await client.stop()
                self.assertIsNotNone(process)
                self.assertIsNotNone(process.returncode)

    async def test_resume_after_restart_and_concurrent_independent_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory))
            first = codex_api.AppServerClient(config)
            await first.start()
            try:
                original = await first.run_turn("one", ephemeral=False)
            finally:
                await first.stop()

            resumed = codex_api.AppServerClient(config)
            await resumed.start()
            try:
                after_restart = await resumed.run_turn("two", thread_id=original.thread_id, ephemeral=False)
                self.assertEqual(after_restart.thread_id, original.thread_id)
                left, right = await asyncio.gather(
                    resumed.run_turn("left", ephemeral=True),
                    resumed.run_turn("right", ephemeral=True),
                )
                self.assertNotEqual(left.thread_id, right.thread_id)
                self.assertEqual({left.text, right.text}, {"answer:left", "answer:right"})
            finally:
                await resumed.stop()

    async def test_threads_disable_unserviceable_approvals_without_changing_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(
                codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}
            ):
                first = codex_api.AppServerClient(config)
                await first.start()
                try:
                    original = await first.run_turn("one", ephemeral=False)
                finally:
                    await first.stop()

                resumed = codex_api.AppServerClient(config)
                await resumed.start()
                try:
                    await resumed.run_turn("two", thread_id=original.thread_id, ephemeral=False)
                finally:
                    await resumed.stop()

            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            start = next(record["params"] for record in records if record["method"] == "thread/start")
            resume = next(record["params"] for record in records if record["method"] == "thread/resume")
            self.assertEqual(start["approvalPolicy"], "never")
            self.assertEqual(resume["approvalPolicy"], "never")
            self.assertEqual(start["sandbox"], "read-only")
            self.assertEqual(resume["sandbox"], "read-only")
            self.assertEqual(
                start,
                {
                    "cwd": str(config.working_directory),
                    "sandbox": "read-only",
                    "ephemeral": False,
                    "approvalPolicy": "never",
                    "model": "gpt-5.6-luna",
                    "config": {
                        "skills": {"config": [
                            {"path": "/fake/skills/existing/SKILL.md", "enabled": False},
                        ]},
                    },
                },
            )
            self.assertEqual(
                resume,
                {
                    "threadId": original.thread_id,
                    "cwd": str(config.working_directory),
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "model": "gpt-5.6-luna",
                    "config": {
                        "skills": {"config": [
                            {"path": "/fake/skills/existing/SKILL.md", "enabled": False},
                        ]},
                    },
                },
            )

    async def test_skills_discovery_disables_future_skills_on_new_and_resumed_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(codex_api.os.environ, {
                "FAKE_APP_SERVER_REQUEST_LOG": str(request_log),
                "FAKE_APP_SERVER_SKILLS": "future",
            }):
                first = codex_api.AppServerClient(config)
                await first.start()
                try:
                    original = await first.run_turn("one", ephemeral=False)
                finally:
                    await first.stop()

                resumed = codex_api.AppServerClient(config)
                await resumed.start()
                try:
                    await resumed.run_turn("two", thread_id=original.thread_id, ephemeral=False)
                finally:
                    await resumed.stop()

            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            expected = [
                {"path": "/fake/skills/existing/SKILL.md", "enabled": False},
                {"path": "/fake/skills/future/SKILL.md", "enabled": False},
            ]
            for method in ("thread/start", "thread/resume"):
                params = next(record["params"] for record in records if record["method"] == method)
                self.assertEqual(params["config"]["skills"]["config"], expected)
                self.assertEqual(params["approvalPolicy"], "never")
                self.assertEqual(params["sandbox"], "read-only")

    async def test_skills_discovery_failure_or_malformed_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory))
            for mode, expected in (("error", "skill discovery failed"), ("malformed", "malformed skills/list")):
                with self.subTest(mode=mode), mock.patch.dict(
                    codex_api.os.environ, {"FAKE_APP_SERVER_SKILLS": mode}
                ):
                    client = codex_api.AppServerClient(config)
                    with self.assertRaisesRegex(codex_api.AppServerUnavailable, expected):
                        await client.start()
                    self.assertFalse(client.healthy)

    async def test_zero_skills_is_a_valid_empty_thread_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(codex_api.os.environ, {
                "FAKE_APP_SERVER_SKILLS": "zero",
                "FAKE_APP_SERVER_REQUEST_LOG": str(request_log),
            }):
                client = codex_api.AppServerClient(settings(root))
                await client.start()
                try:
                    await client.run_turn("zero")
                finally:
                    await client.stop()
            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            started = next(record["params"] for record in records if record["method"] == "thread/start")
            self.assertEqual(started["config"], {"skills": {"config": []}})

    async def test_skills_changed_invalidates_cached_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(codex_api.os.environ, {
                "FAKE_APP_SERVER_REQUEST_LOG": str(request_log),
            }):
                client = codex_api.AppServerClient(settings(root))
                await client.start()
                try:
                    await client.run_turn("one")
                    await client._dispatch({"method": "skills/changed", "params": {}})
                    await client.run_turn("two")
                finally:
                    await client.stop()
            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(record["method"] == "skills/list" for record in records), 2)

    async def test_timeout_malformed_json_and_child_death_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory), timeout=0.03)
            client = codex_api.AppServerClient(config)
            await client.start()
            try:
                with self.assertRaisesRegex(codex_api.AppServerError, "exceeded"):
                    await client.run_turn("HANG")
            finally:
                await client.stop()

            malformed = codex_api.AppServerClient(config)
            await malformed.start()
            try:
                with self.assertRaisesRegex(codex_api.AppServerError, "malformed"):
                    await malformed.run_turn("MALFORMED")
                self.assertFalse(malformed.healthy)
            finally:
                await malformed.stop()

            dead = codex_api.AppServerClient(config)
            await dead.start()
            try:
                with self.assertRaises(codex_api.AppServerError):
                    await dead.run_turn("DIE")
                self.assertFalse(dead.healthy)
            finally:
                await dead.stop()

    async def test_native_compaction_drains_its_turn_before_the_next_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}):
                client = codex_api.AppServerClient(settings(root))
                await client.start()
                try:
                    await client.run_turn("first", ephemeral=False)
                    compacted = await client.compact_thread("thread-1")
                    result = await client.run_turn("after", thread_id="thread-1", ephemeral=False)
                finally:
                    await client.stop()
            self.assertEqual(compacted.usage["input_tokens"], 7)
            self.assertEqual(result.text, "answer:after")
            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            methods = [record["method"] for record in records]
            self.assertLess(methods.index("thread/compact/start"), len(methods) - 1)
            self.assertEqual(methods[methods.index("thread/compact/start") + 1], "turn/start")

    async def test_native_compaction_timeout_failure_and_malformed_notifications_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory), timeout=0.03)
            for mode, expected in (("hang", "compaction exceeded"), ("failed", "compaction did not complete"),
                                   ("malformed", "malformed compaction notification")):
                with self.subTest(mode=mode), mock.patch.dict(codex_api.os.environ, {"FAKE_APP_SERVER_COMPACTION": mode}):
                    client = codex_api.AppServerClient(config)
                    await client.start()
                    try:
                        await client.run_turn("first", ephemeral=False)
                        with self.assertRaisesRegex(codex_api.AppServerError, expected):
                            await client.compact_thread("thread-1")
                    finally:
                        await client.stop()


class AppServerHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.codex_home = tempfile.TemporaryDirectory()
        self.codex_home_patch = mock.patch.dict(
            codex_api.os.environ, {"CODEX_HOME": self.codex_home.name}
        )
        self.codex_home_patch.start()

    def tearDown(self):
        self.codex_home_patch.stop()
        self.codex_home.cleanup()

    async def test_asgi_lifespan_starts_and_stops_the_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            app = codex_api.CodexApi(settings(Path(directory)))
            received = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
            sent = []

            async def receive():
                return next(received)

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan"}, receive, send)
            self.assertEqual([item["type"] for item in sent], [
                "lifespan.startup.complete", "lifespan.shutdown.complete",
            ])
            self.assertFalse(app.app_server.healthy)

    async def test_both_http_endpoints_and_store_false(self):
        with tempfile.TemporaryDirectory() as directory:
            app = codex_api.CodexApi(settings(Path(directory)))
            await app.start()
            try:
                status, chat = await request(app, "/v1/chat/completions", {"messages": [{"role": "user", "content": "hello"}]})
                self.assertEqual(status, 200)
                self.assertIn("answer:", chat["choices"][0]["message"]["content"])
                self.assertEqual(chat["usage"]["prompt_tokens_details"]["cached_tokens"], 4)

                status, stored = await request(app, "/v1/responses", {"input": "one"})
                self.assertEqual(status, 200)
                response_id = stored["id"]
                self.assertIn(response_id, app.response_state.responses)
                self.assertEqual(stored["usage"]["input_tokens"], 10)

                status, unstored = await request(
                    app, "/v1/responses",
                    {"input": "two", "previous_response_id": response_id, "store": False},
                )
                self.assertEqual(status, 200)
                self.assertFalse(unstored["store"])
                self.assertNotIn(unstored["id"], app.response_state.responses)
                self.assertEqual(unstored["usage"]["output_tokens"], 3)
            finally:
                await app.stop()

    async def test_responses_plain_string_reaches_app_server_without_a_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(
                codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}
            ):
                app = codex_api.CodexApi(settings(root))
                await app.start()
                try:
                    status, body = await request(
                        app, "/v1/responses", {"input": "  <user>exact</user>\n "}
                    )
                    self.assertEqual(status, 200)
                    self.assertEqual(body["output"][0]["content"][0]["text"], "answer:  <user>exact</user>")
                finally:
                    await app.stop()

            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            turn = next(record["params"] for record in records if record["method"] == "turn/start")
            self.assertEqual(turn["input"], [{"type": "text", "text": "  <user>exact</user>\n "}])

    async def test_health_reports_backend_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            app = codex_api.CodexApi(settings(Path(directory)))
            sent = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await app({"type": "http", "method": "GET", "path": "/health", "headers": []}, receive, send)
            self.assertEqual(sent[0]["status"], 503)

    async def test_responses_context_management_compacts_then_aggregates_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}):
                app = codex_api.CodexApi(settings(root))
                await app.start()
                try:
                    _, first = await request(app, "/v1/responses", {"input": "first"})
                    status, body = await request(app, "/v1/responses", {
                        "input": "second", "previous_response_id": first["id"],
                        "context_management": [{"type": "compaction", "compact_threshold": 10}],
                    })
                finally:
                    await app.stop()
            self.assertEqual(status, 200)
            self.assertEqual(body["usage"]["input_tokens"], 17)
            self.assertEqual(body["usage"]["input_tokens_details"]["cached_tokens"], 6)
            self.assertEqual(body["usage"]["output_tokens"], 5)
            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["method"] for r in records].count("thread/compact/start"), 1)
            self.assertEqual(app.response_state.context_input_tokens_for(body["id"]), 10)

    async def test_responses_context_management_below_threshold_and_invalid_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_log = root / "requests.jsonl"
            with mock.patch.dict(codex_api.os.environ, {"FAKE_APP_SERVER_REQUEST_LOG": str(request_log)}):
                app = codex_api.CodexApi(settings(root))
                await app.start()
                try:
                    _, first = await request(app, "/v1/responses", {"input": "first"})
                    status, _ = await request(app, "/v1/responses", {
                        "input": "second", "previous_response_id": first["id"],
                        "context_management": [{"type": "compaction", "compact_threshold": 11}],
                    })
                    invalid_status, invalid = await request(app, "/v1/responses", {
                        "input": "bad", "context_management": {"type": "compaction"},
                    })
                finally:
                    await app.stop()
            self.assertEqual(status, 200)
            self.assertEqual(invalid_status, 400)
            self.assertIn("must be an array", invalid["error"]["message"])
            records = [json.loads(line) for line in request_log.read_text(encoding="utf-8").splitlines()]
            self.assertNotIn("thread/compact/start", [r["method"] for r in records])


class SameThreadSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_response_thread_is_serialized(self):
        class Backend:
            healthy = True
            failure = None

            def __init__(self):
                self.active = 0
                self.maximum = 0

            async def run_turn(self, prompt, *, thread_id=None, ephemeral=True):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0.02)
                self.active -= 1
                return codex_api.TurnResult(
                    "answer", {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
                    {"input_tokens": 2, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_output_tokens": 0},
                    thread_id or "new-thread",
                )

            async def compact_thread(self, thread_id):
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                await asyncio.sleep(0.02)
                self.active -= 1
                return codex_api.CompactionResult(
                    {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
                    {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
                )

            async def start(self):
                pass

            async def stop(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            backend = Backend()
            app = codex_api.CodexApi(settings(Path(directory)), app_server=backend)
            app.response_state.remember("previous", "thread-1", {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0}, 1)
            first, second = await asyncio.gather(
                request(app, "/v1/responses", {"input": "a", "previous_response_id": "previous", "context_management": [{"type": "compaction", "compact_threshold": 1}]}),
                request(app, "/v1/responses", {"input": "b", "previous_response_id": "previous", "context_management": [{"type": "compaction", "compact_threshold": 1}]}),
            )
            self.assertEqual(first[0], 200)
            self.assertEqual(second[0], 200)
            self.assertEqual(backend.maximum, 1)


if __name__ == "__main__":
    unittest.main()
