#!/usr/bin/env python3
"""Expose Codex CLI through a small OpenAI-compatible API.

This is intended for local clients that know how to call Chat Completions or
Responses endpoints but cannot invoke Codex CLI directly. Chat Completions are
stateless. Responses can continue a persisted Codex session by passing the
previous response's ID as ``previous_response_id``.

The listener is local-only by default. See ``--help`` for configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hmac
import json
import logging
import os
import re
import tempfile
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection

import uvicorn
import yaml


AsgiReceive = Callable[[], Awaitable[dict[str, Any]]]
AsgiSend = Callable[[dict[str, Any]], Awaitable[None]]

LOG = logging.getLogger("codex-api")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
LEAN_PROFILE_NAME = "codex-api-lean"
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    codex_binary: str
    working_directory: Path
    sandbox: str
    timeout_seconds: float
    model: str | None
    thinking_effort: str | None
    profile_instructions: Path
    bearer_token: str | None
    max_concurrent_requests: int
    state_file: Path
    app_server_start_timeout: float = 30.0
    log_level: str = "info"
    bearer_tokens: frozenset[str] = field(default_factory=frozenset)


def text_from_content(content: Any) -> str:
    """Extract text from common Chat Completions content formats."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            item_type = item.get("type")
            if item_type in {"text", "input_text", "output_text"}:
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "\n".join(parts)


def prompt_from_messages(messages: Any) -> str:
    """Render a chat history as a single unambiguous prompt for Codex."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("`messages` must be a non-empty array")

    sections = [
        "Respond to the conversation below. Return only the assistant's answer "
        "to the final user message; do not describe this wrapper or the role labels."
    ]
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object")
        role = message.get("role", "user")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError(f"messages[{index}].role is unsupported: {role!r}")
        content = text_from_content(message.get("content"))
        if not content and role == "tool":
            content = str(message.get("content") or "")
        sections.append(f"<{role}>\n{content}\n</{role}>")
    return "\n\n".join(sections)


def prompt_from_responses_request(request: dict[str, Any]) -> str:
    """Render supported Responses API text input as one Codex turn."""
    input_value = request.get("input", "")
    instructions = request.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise ValueError("`instructions` must be a string or null")

    # The lean base instructions already establish the text-only response
    # contract.  Preserve a plain caller string byte-for-byte when there is no
    # top-level instruction that needs an unambiguous role boundary.  This is
    # deliberately limited to Responses: Chat Completions are stateless and
    # must continue rendering their complete role-labelled history.
    if "input" in request and isinstance(input_value, str) and instructions is None:
        return input_value

    sections = [
        "Respond to the input below. Return only the assistant's answer; do not "
        "describe this wrapper or the role labels."
    ]
    if instructions:
        sections.append(f"<developer>\n{instructions}\n</developer>")

    if isinstance(input_value, str):
        sections.append(f"<user>\n{input_value}\n</user>")
        return "\n\n".join(sections)
    if not isinstance(input_value, list):
        raise ValueError("`input` must be a string or an array of text messages")

    for index, item in enumerate(input_value):
        if not isinstance(item, dict):
            raise ValueError(f"input[{index}] must be a message object")
        item_type = item.get("type", "message")
        if item_type != "message":
            raise ValueError(f"input[{index}].type is unsupported: {item_type!r}")
        role = item.get("role", "user")
        if role not in {"system", "developer", "user", "assistant"}:
            raise ValueError(f"input[{index}].role is unsupported: {role!r}")
        text = text_from_content(item.get("content"))
        sections.append(f"<{role}>\n{text}\n</{role}>")
    return "\n\n".join(sections)


USAGE_COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "cache_write_input_tokens",
)
REQUIRED_USAGE_COUNTERS = USAGE_COUNTERS[:4]


def usage_counter(usage: dict[str, Any], name: str) -> int | None:
    """Return a non-negative integer counter, or ``None`` when it is unusable."""
    value = usage.get(name)
    if isinstance(value, bool):
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if integer >= 0 else None


def normalized_usage(codex_usage: dict[str, Any]) -> dict[str, Any]:
    """Translate Codex's JSONL usage event to Chat Completions-style fields."""
    # The caller validates deltas.  Direct usage also goes through this helper,
    # so malformed counters can never make an API response negative.
    input_tokens = usage_counter(codex_usage, "input_tokens") or 0
    cached_input_tokens = usage_counter(codex_usage, "cached_input_tokens") or 0
    output_tokens = usage_counter(codex_usage, "output_tokens") or 0
    reasoning_tokens = usage_counter(codex_usage, "reasoning_output_tokens") or 0
    input_details = {"cached_tokens": cached_input_tokens}
    cache_write_tokens = usage_counter(codex_usage, "cache_write_input_tokens")
    if cache_write_tokens is not None:
        # This is a harmless extension for Codex versions that expose this
        # counter; OpenAI clients that do not know it ignore the detail member.
        input_details["cache_write_tokens"] = cache_write_tokens
    return {
        "prompt_tokens": input_tokens,
        # Codex's output_tokens already includes the reasoning-token subset.
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "prompt_tokens_details": input_details,
        "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
    }


def incremental_usage(
    cumulative_usage: dict[str, Any], previous_cumulative_usage: dict[str, Any] | None
) -> dict[str, Any]:
    """Return a per-turn delta, or safely fall back to direct counters.

    Older state records have no usage snapshot.  In that case Codex's direct
    totals remain the only available value.  We log this explicitly rather than
    pretending that a cumulative total is a turn delta.
    """
    if previous_cumulative_usage is None:
        LOG.warning(
            "Cannot calculate resumed-turn token delta: predecessor has no cumulative usage; "
            "returning sanitized Codex totals"
        )
        return cumulative_usage

    current_values = {name: usage_counter(cumulative_usage, name) for name in REQUIRED_USAGE_COUNTERS}
    previous_values = {
        name: usage_counter(previous_cumulative_usage, name) for name in REQUIRED_USAGE_COUNTERS
    }
    if any(value is None for value in current_values.values()) or any(
        value is None for value in previous_values.values()
    ):
        LOG.warning(
            "Cannot calculate resumed-turn token delta due to missing or malformed cumulative "
            "counters; returning sanitized Codex totals"
        )
        return cumulative_usage
    if any(current_values[name] < previous_values[name] for name in REQUIRED_USAGE_COUNTERS):
        LOG.warning(
            "Cannot calculate resumed-turn token delta because cumulative counters regressed; "
            "returning sanitized Codex totals"
        )
        return cumulative_usage

    delta = dict(cumulative_usage)
    for name in REQUIRED_USAGE_COUNTERS:
        delta[name] = current_values[name] - previous_values[name]
    # Codex versions which emit cache-write totals get the same correction,
    # but a missing counter is not allowed to invalidate the main token delta.
    current_cache_write = usage_counter(cumulative_usage, "cache_write_input_tokens")
    previous_cache_write = usage_counter(previous_cumulative_usage, "cache_write_input_tokens")
    if current_cache_write is not None and previous_cache_write is not None:
        if current_cache_write < previous_cache_write:
            LOG.warning("Codex cache-write cumulative counter regressed; omitting cache-write delta")
            delta.pop("cache_write_input_tokens", None)
        else:
            delta["cache_write_input_tokens"] = current_cache_write - previous_cache_write
    elif current_cache_write is not None:
        LOG.warning("Cannot calculate Codex cache-write delta; omitting cache-write counter")
        delta.pop("cache_write_input_tokens", None)
    return delta


def combined_usage(*usages: dict[str, Any]) -> dict[str, Any]:
    """Add independent App Server passes without inventing unavailable counters."""
    result: dict[str, Any] = {}
    for name in USAGE_COUNTERS:
        values = [usage_counter(usage, name) for usage in usages]
        # Main counters are always present in the installed protocol.  The
        # optional cache-write counter is omitted unless every pass exposed it.
        if any(value is None for value in values):
            continue
        result[name] = sum(values)
    return result


def compaction_threshold(context_management: Any) -> int | None:
    """Validate the documented server-side Responses compaction selector.

    The OpenAI API represents this as an array of context-management objects.
    This text-only proxy has one native compaction mechanism, so accepting more
    than one selector would have ambiguous threshold semantics.
    """
    if context_management is None:
        return None
    if not isinstance(context_management, list):
        raise ValueError("`context_management` must be an array")
    if len(context_management) != 1:
        raise ValueError("`context_management` must contain exactly one compaction object")
    setting = context_management[0]
    if not isinstance(setting, dict):
        raise ValueError("`context_management[0]` must be an object")
    if setting.get("type") != "compaction":
        raise ValueError("`context_management[0].type` must be `compaction`")
    threshold = setting.get("compact_threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        raise ValueError("`context_management[0].compact_threshold` must be a positive integer")
    if set(setting) != {"type", "compact_threshold"}:
        raise ValueError("`context_management[0]` contains unsupported fields")
    return threshold


def responses_usage(chat_usage: dict[str, Any]) -> dict[str, Any]:
    """Translate normalized token counts to the Responses API usage shape."""
    return {
        "input_tokens": chat_usage["prompt_tokens"],
        "input_tokens_details": chat_usage["prompt_tokens_details"],
        "output_tokens": chat_usage["completion_tokens"],
        "output_tokens_details": chat_usage["completion_tokens_details"],
        "total_tokens": chat_usage["total_tokens"],
    }


def completion_body(text: str, requested_model: str, usage: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": now,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def response_body(
    text: str,
    requested_model: str,
    usage: dict[str, Any],
    response_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    message_id = f"msg_{uuid.uuid4().hex}"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": request.get("instructions"),
        "max_output_tokens": request.get("max_output_tokens"),
        "metadata": request.get("metadata") or {},
        "model": requested_model,
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": bool(request.get("parallel_tool_calls", True)),
        "previous_response_id": request.get("previous_response_id"),
        "store": request.get("store") is not False,
        "temperature": request.get("temperature"),
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": [],
        "top_p": request.get("top_p"),
        "truncation": request.get("truncation", "disabled"),
        "usage": responses_usage(usage),
        "user": request.get("user"),
    }


def error_body(
    message: str,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "param": param, "code": code}}


class ResponseState:
    """Persist the public response IDs that point to saved Codex threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.responses: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            responses = payload.get("responses")
            if not isinstance(responses, dict):
                raise ValueError("missing `responses` object")
            self.responses = responses
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Could not load response state file {self.path}: {error}") from error

    def thread_for(self, response_id: str) -> str | None:
        record = self.responses.get(response_id)
        if not isinstance(record, dict):
            return None
        thread_id = record.get("thread_id")
        return thread_id if isinstance(thread_id, str) else None

    def cumulative_usage_for(self, response_id: str) -> dict[str, Any] | None:
        """Return a copied usage snapshot, accepting version-1 thread records."""
        record = self.responses.get(response_id)
        if not isinstance(record, dict):
            return None
        usage = record.get("cumulative_usage")
        return dict(usage) if isinstance(usage, dict) else None

    def context_input_tokens_for(self, response_id: str) -> int | None:
        """Return the predecessor's latest rendered context size, if recorded.

        Version-1/2 records intentionally return ``None``: their cumulative
        counters cannot reliably say how large the active context was.
        """
        record = self.responses.get(response_id)
        if not isinstance(record, dict):
            return None
        value = record.get("context_input_tokens")
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def remember(
        self,
        response_id: str,
        thread_id: str,
        cumulative_usage: dict[str, Any],
        context_input_tokens: int | None = None,
    ) -> None:
        record = {
            "thread_id": thread_id,
            "cumulative_usage": cumulative_usage,
            "created_at": int(time.time()),
        }
        if context_input_tokens is not None:
            record["context_input_tokens"] = context_input_tokens
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_path.write_text(
                json.dumps({"version": 3, "responses": {**self.responses, response_id: record}}, indent=2)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self.path)
            self.responses[response_id] = record
        except OSError:
            raise
        finally:
            temporary_path.unlink(missing_ok=True)


class AppServerError(RuntimeError):
    """A failure in the local App Server transport or protocol."""


class AppServerUnavailable(AppServerError):
    """The child is not initialized or has exited."""


@dataclass(frozen=True)
class TurnResult:
    text: str
    usage: dict[str, Any]
    cumulative_usage: dict[str, Any]
    thread_id: str


@dataclass(frozen=True)
class CompactionResult:
    usage: dict[str, Any]
    cumulative_usage: dict[str, Any]


def app_server_usage(breakdown: Any) -> dict[str, Any] | None:
    """Convert the v2 `TokenUsageBreakdown` shape to the existing CLI shape."""
    if not isinstance(breakdown, dict):
        return None
    names = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
    }
    result: dict[str, Any] = {}
    for source, target in names.items():
        value = usage_counter(breakdown, source)
        if value is None:
            return None
        result[target] = value
    return result


class AppServerClient:
    """One asynchronous JSON-RPC connection to local ``codex app-server``.

    The 0.144.6 protocol is line-delimited JSON on stdio.  A single reader owns
    stdout, resolves request futures, and routes per-thread notifications to a
    queue.  This avoids racing independent HTTP requests over the one pipe.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self._next_request_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._thread_events: dict[str, asyncio.Queue[dict[str, Any] | BaseException]] = {}
        self._loaded_threads: set[str] = set()
        self._disabled_skills_config: dict[str, Any] | None = None
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._skills_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready = False
        self._stopping = False
        self.failure: str | None = None

    @property
    def healthy(self) -> bool:
        return bool(self._ready and self.process and self.process.returncode is None)

    def _command(self) -> list[str]:
        command = [self.settings.codex_binary]
        # Codex 0.144.6 intentionally limits `--profile` to runtime commands,
        # and rejects it for `app-server`.  Apply that profile's TOML settings
        # as documented `--config` overrides instead, without touching the
        # user's base config or catalog.
        for key, value in _profile_config_overrides(LEAN_PROFILE_NAME):
            command.extend(["--config", f"{key}={_toml_literal(value)}"])
        # Config tables merge across Codex's user, project, profile, and CLI
        # layers.  An empty `mcp_servers` table would therefore leave inherited
        # entries alive.  Enumerate every reachable configured server and set
        # its leaf `enabled` value at CLI-override precedence *before* the App
        # Server starts, so no MCP child or connection is created for the proxy.
        mcp_names = _configured_mcp_server_names(self.settings.working_directory, LEAN_PROFILE_NAME)
        if mcp_names:
            command.extend(["--config", _mcp_servers_disabled_override(mcp_names)])
        if self.settings.thinking_effort:
            command.extend(
                ["--config", f'model_reasoning_effort="{self.settings.thinking_effort}"']
            )
        command.extend(["app-server", "--stdio"])
        return command

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.healthy:
                return
            if self.process and self.process.returncode is None:
                raise AppServerUnavailable(self.failure or "Codex App Server is not initialized")
            self.failure = None
            self._stopping = False
            self._loaded_threads.clear()
            self._disabled_skills_config = None
            try:
                _ensure_lean_profile(self.settings.profile_instructions)
                LOG.info("Starting persistent Codex App Server in %s", self.settings.working_directory)
                self.process = await asyncio.create_subprocess_exec(
                    *self._command(),
                    cwd=self.settings.working_directory,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as error:
                raise AppServerUnavailable(
                    f"Codex executable was not found: {self.settings.codex_binary}"
                ) from error
            assert self.process.stdout and self.process.stderr
            self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-app-server-stdout")
            self._stderr_task = asyncio.create_task(self._drain_stderr(), name="codex-app-server-stderr")
            try:
                await self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex-api-bypass",
                            "title": "Codex API Bypass",
                            "version": "1.0",
                        }
                    },
                    timeout=self.settings.app_server_start_timeout,
                    allow_before_ready=True,
                )
                await self.notify("initialized", {})
                self._ready = True
                # Disabled MCP servers must not even be initialized: merely
                # hiding their tools at thread setup would still leak server
                # instructions/resources and permit startup side effects.
                await self._verify_no_mcp_servers()
                # Discover the exact skill set for this working directory
                # before serving any turn.  A discovery failure is fail-closed:
                # this text-only proxy must not silently expose a skills prompt.
                await self._disabled_skills_thread_config()
                LOG.info("Codex App Server initialized")
            except (AppServerError, asyncio.TimeoutError) as error:
                await self._stop_process()
                raise AppServerUnavailable(f"Could not initialize Codex App Server: {error}") from error

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_process()

    async def _stop_process(self) -> None:
        """Stop the child. The caller holds `_lifecycle_lock` when required."""
        self._stopping = True
        self._ready = False
        process = self.process
        if not process:
            return
        if process.stdin and not process.stdin.is_closing():
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            LOG.warning("Codex App Server did not exit within 5 seconds; terminating it")
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
        self.process = None
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(AppServerUnavailable("Codex App Server stopped"))
        self._loaded_threads.clear()
        self._disabled_skills_config = None

    async def _verify_no_mcp_servers(self) -> None:
        """Fail closed unless the process-local overrides yielded no MCP inventory."""
        result = await self.request(
            "mcpServerStatus/list",
            {"detail": "toolsAndAuthOnly"},
            timeout=self.settings.app_server_start_timeout,
        )
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise AppServerError("Codex App Server returned malformed mcpServerStatus/list results")
        if result.get("nextCursor") is not None:
            raise AppServerError("Codex App Server returned paginated MCP inventory; refusing proxy startup")
        for status in result["data"]:
            # The installed status method includes configured-but-disabled
            # names.  Those are safe only when it exposes no initialized
            # server metadata, tools, resources, or resource templates.
            if not isinstance(status, dict) or not isinstance(status.get("name"), str):
                raise AppServerError("Codex App Server returned malformed MCP server status")
            if (
                status.get("serverInfo") is not None
                or status.get("tools") != {}
                or status.get("resources") != []
                or status.get("resourceTemplates") != []
            ):
                raise AppServerError("Codex App Server still exposes an MCP server; refusing proxy startup")

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        allow_before_ready: bool = False,
    ) -> Any:
        if not allow_before_ready and not self.healthy:
            raise AppServerUnavailable(self.failure or "Codex App Server is unavailable")
        process = self.process
        if not process or not process.stdin or process.stdin.is_closing():
            raise AppServerUnavailable("Codex App Server stdin is unavailable")
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"method": method, "id": request_id, "params": params})
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as error:
            raise AppServerError(f"Codex App Server timed out waiting for {method}") from error
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin or self.process.stdin.is_closing():
            raise AppServerUnavailable("Codex App Server stdin is unavailable")
        await self._write({"method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self.process.stdin.write(encoded)
            try:
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                self._fatal(AppServerUnavailable("Codex App Server stdin closed"))
                raise AppServerUnavailable("Codex App Server stdin closed") from error

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as error:
                    self._fatal(AppServerError("Codex App Server emitted malformed JSON-RPC output"))
                    LOG.error("Malformed App Server JSON: %s", line[:200].decode("utf-8", "replace"))
                    return
                if not isinstance(message, dict):
                    self._fatal(AppServerError("Codex App Server emitted a non-object JSON-RPC message"))
                    return
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - defensive pipe failure path
            self._fatal(AppServerError(f"Codex App Server stdout reader failed: {error}"))
        finally:
            if not self._stopping:
                return_code = self.process.returncode if self.process else None
                self._fatal(AppServerUnavailable(f"Codex App Server exited unexpectedly ({return_code})"))

    async def _dispatch(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if isinstance(message_id, int) and "method" not in message:
            future = self._pending.get(message_id)
            if future is None:
                LOG.warning("Ignoring App Server response for unknown request id %s", message_id)
                return
            if "error" in message:
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else str(error)
                future.set_exception(AppServerError(f"Codex App Server RPC error: {detail}"))
            elif "result" in message:
                future.set_result(message["result"])
            else:
                future.set_exception(AppServerError("Malformed App Server response without result or error"))
            return
        method = message.get("method")
        if not isinstance(method, str):
            self._fatal(AppServerError("Malformed App Server JSON-RPC message"))
            return
        # The lean profile has no tools that should make server requests.  If a
        # future Codex release sends one, reject it safely rather than leave the
        # JSON-RPC peer hanging.
        if isinstance(message_id, int):
            await self._write(
                {"id": message_id, "error": {"code": -32601, "message": "Unsupported by codex-api"}}
            )
            return
        params = message.get("params")
        if not isinstance(params, dict):
            LOG.warning("Ignoring malformed App Server notification %s", method)
            return
        if method == "skills/changed":
            # The installed protocol documents this notification as an
            # invalidation signal.  The next thread creation/resume refreshes
            # its complete disable list before it can start a model turn.
            self._disabled_skills_config = None
            LOG.info("Codex App Server skill set changed; disabling configuration will refresh")
            return
        thread_id = params.get("threadId")
        if isinstance(thread_id, str):
            self._thread_events.setdefault(thread_id, asyncio.Queue()).put_nowait(message)
        elif method == "error":
            LOG.warning("Codex App Server notification: %s", _safe_log_value(params))

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        try:
            while line := await self.process.stderr.readline():
                LOG.warning("Codex App Server stderr: %s", _safe_log_value(line.decode("utf-8", "replace")))
        except asyncio.CancelledError:
            raise

    def _fatal(self, error: AppServerError) -> None:
        if self.failure is not None:
            return
        self.failure = str(error)
        self._ready = False
        self._fail_pending(error)
        for queue in self._thread_events.values():
            queue.put_nowait(error)

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def run_turn(
        self, prompt: str, *, thread_id: str | None = None, ephemeral: bool = True
    ) -> TurnResult:
        if not self.healthy:
            raise AppServerUnavailable(self.failure or "Codex App Server is unavailable")
        if thread_id is None:
            thread_id = await self._start_thread(ephemeral)
        elif thread_id not in self._loaded_threads:
            await self._resume_thread(thread_id)
        queue = self._thread_events.setdefault(thread_id, asyncio.Queue())
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(self.settings.working_directory),
        }
        if self.settings.model:
            turn_params["model"] = self.settings.model
        if self.settings.thinking_effort:
            turn_params["effort"] = self.settings.thinking_effort
        started = await self.request("turn/start", turn_params, timeout=self.settings.timeout_seconds)
        if not isinstance(started, dict) or not isinstance(started.get("turn"), dict):
            raise AppServerError("Codex App Server returned no turn from turn/start")
        turn_id = started["turn"].get("id")
        if not isinstance(turn_id, str):
            raise AppServerError("Codex App Server returned a turn without an ID")
        texts = _agent_message_texts(started["turn"].get("items"))
        last_usage: dict[str, Any] | None = None
        total_usage: dict[str, Any] | None = None
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=self.settings.timeout_seconds)
                if isinstance(event, BaseException):
                    raise event
                params = event["params"]
                event_turn_id = params.get("turnId")
                if event["method"] == "turn/completed" and isinstance(params.get("turn"), dict):
                    event_turn_id = params["turn"].get("id")
                if event_turn_id != turn_id:
                    continue
                if event["method"] == "item/completed":
                    texts.extend(_agent_message_texts([params.get("item")]))
                elif event["method"] == "thread/tokenUsage/updated":
                    token_usage = params.get("tokenUsage")
                    if isinstance(token_usage, dict):
                        last_usage = app_server_usage(token_usage.get("last"))
                        total_usage = app_server_usage(token_usage.get("total"))
                elif event["method"] == "turn/completed":
                    turn = params.get("turn")
                    if isinstance(turn, dict):
                        texts.extend(_agent_message_texts(turn.get("items")))
                        if turn.get("status") != "completed":
                            detail = turn.get("error") or turn.get("status")
                            raise AppServerError(f"Codex App Server turn did not complete: {detail}")
                    if not last_usage or not total_usage:
                        raise AppServerError("Codex App Server completed without token-usage notification")
                    text = "\n".join(dict.fromkeys(texts)).strip()
                    if not text:
                        raise AppServerError("Codex App Server completed without a final assistant message")
                    return TurnResult(text, last_usage, total_usage, thread_id)
        except asyncio.TimeoutError as error:
            with contextlib.suppress(AppServerError, asyncio.TimeoutError):
                await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
            raise AppServerError(
                f"Codex App Server turn exceeded the {self.settings.timeout_seconds:g}-second timeout"
            ) from error

    async def compact_thread(self, thread_id: str) -> CompactionResult:
        """Run and drain the installed native asynchronous compaction lifecycle.

        `thread/compact/start` acknowledges scheduling only.  The actual work
        has its own `turn/started`, token-usage update, context-compaction item,
        and terminal `turn/completed` notifications on the normal per-thread
        queue.  Draining all of them before returning prevents those events from
        being consumed by the next user turn.
        """
        if not self.healthy:
            raise AppServerUnavailable(self.failure or "Codex App Server is unavailable")
        if thread_id not in self._loaded_threads:
            await self._resume_thread(thread_id)
        queue = self._thread_events.setdefault(thread_id, asyncio.Queue())
        await self.request(
            "thread/compact/start", {"threadId": thread_id}, timeout=self.settings.timeout_seconds
        )
        compact_turn_id: str | None = None
        last_usage: dict[str, Any] | None = None
        total_usage: dict[str, Any] | None = None
        saw_context_compaction = False
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=self.settings.timeout_seconds)
                if isinstance(event, BaseException):
                    raise event
                method = event.get("method")
                params = event.get("params")
                if not isinstance(params, dict):
                    raise AppServerError("Codex App Server emitted malformed compaction notification")
                if method == "turn/started":
                    turn = params.get("turn")
                    candidate = turn.get("id") if isinstance(turn, dict) else None
                    if not isinstance(candidate, str):
                        raise AppServerError("Codex App Server started compaction without a turn ID")
                    compact_turn_id = candidate
                    continue
                event_turn_id = params.get("turnId")
                if method == "turn/completed" and isinstance(params.get("turn"), dict):
                    event_turn_id = params["turn"].get("id")
                if compact_turn_id is None or event_turn_id != compact_turn_id:
                    continue
                if method == "item/completed":
                    item = params.get("item")
                    completed_at = params.get("completedAtMs")
                    if isinstance(completed_at, bool) or not isinstance(completed_at, int):
                        raise AppServerError("Codex App Server emitted malformed compaction notification")
                    if isinstance(item, dict) and item.get("type") == "contextCompaction":
                        saw_context_compaction = True
                elif method == "thread/compacted":
                    # Kept for compatibility with the installed schema's
                    # deprecated notification while preferring the item event.
                    saw_context_compaction = True
                elif method == "thread/tokenUsage/updated":
                    token_usage = params.get("tokenUsage")
                    if isinstance(token_usage, dict):
                        last_usage = app_server_usage(token_usage.get("last"))
                        total_usage = app_server_usage(token_usage.get("total"))
                elif method == "turn/completed":
                    turn = params.get("turn")
                    if not isinstance(turn, dict) or turn.get("status") != "completed":
                        detail = turn.get("error") if isinstance(turn, dict) else None
                        raise AppServerError(f"Codex App Server compaction did not complete: {detail or 'unknown'}")
                    if not saw_context_compaction:
                        raise AppServerError("Codex App Server compaction completed without a context-compaction event")
                    if not last_usage or not total_usage:
                        raise AppServerError("Codex App Server compaction completed without token-usage notification")
                    return CompactionResult(last_usage, total_usage)
        except asyncio.TimeoutError as error:
            if compact_turn_id:
                with contextlib.suppress(AppServerError, asyncio.TimeoutError):
                    await self.request(
                        "turn/interrupt", {"threadId": thread_id, "turnId": compact_turn_id}, timeout=5
                    )
            raise AppServerError(
                f"Codex App Server compaction exceeded the {self.settings.timeout_seconds:g}-second timeout"
            ) from error

    async def _start_thread(self, ephemeral: bool) -> str:
        params: dict[str, Any] = {
            "cwd": str(self.settings.working_directory),
            "sandbox": self.settings.sandbox,
            "ephemeral": ephemeral,
            # This text-only proxy cannot present or service App Server
            # approval requests.  Keep that host-side policy independent from
            # the configured filesystem sandbox.
            "approvalPolicy": "never",
        }
        if self.settings.model:
            params["model"] = self.settings.model
        params["config"] = await self._disabled_skills_thread_config()
        result = await self.request("thread/start", params, timeout=self.settings.timeout_seconds)
        return self._remember_thread(result, "thread/start")

    async def _resume_thread(self, thread_id: str) -> None:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.settings.working_directory),
            "sandbox": self.settings.sandbox,
            # A persisted rollout may have been created with a different
            # policy, so reassert the proxy's non-interactive policy on resume.
            "approvalPolicy": "never",
        }
        if self.settings.model:
            params["model"] = self.settings.model
        params["config"] = await self._disabled_skills_thread_config()
        result = await self.request("thread/resume", params, timeout=self.settings.timeout_seconds)
        resumed_id = self._remember_thread(result, "thread/resume")
        if resumed_id != thread_id:
            raise AppServerError("Codex App Server resumed a different thread than requested")

    def _remember_thread(self, result: Any, method: str) -> str:
        if not isinstance(result, dict) or not isinstance(result.get("thread"), dict):
            raise AppServerError(f"Codex App Server returned no thread from {method}")
        thread_id = result["thread"].get("id")
        if not isinstance(thread_id, str):
            raise AppServerError(f"Codex App Server returned a thread without an ID from {method}")
        self._loaded_threads.add(thread_id)
        self._thread_events.setdefault(thread_id, asyncio.Queue())
        return thread_id

    async def _disabled_skills_thread_config(self) -> dict[str, Any]:
        """Discover and disable every skill visible in this proxy's CWD.

        `thread/start.config` and `thread/resume.config` accept regular Codex
        configuration values.  The installed v2 `skills/list` response exposes
        each skill's stable path, which is the selector accepted by
        `skills.config`; names are deliberately not hard-coded so future CLI
        upgrades cannot advertise newly added skills to a proxy thread.
        """
        async with self._skills_lock:
            if self._disabled_skills_config is not None:
                return copy.deepcopy(self._disabled_skills_config)

            result = await self.request(
                "skills/list",
                {"cwds": [str(self.settings.working_directory)]},
                timeout=self.settings.timeout_seconds,
            )
            config = self._disabled_skills_config_from_result(result)
            self._disabled_skills_config = config
            return copy.deepcopy(config)

    def _disabled_skills_config_from_result(self, result: Any) -> dict[str, Any]:
        """Validate `skills/list` strictly enough to preserve fail-closed behavior."""
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise AppServerError("Codex App Server returned malformed skills/list results")

        expected_cwd = self.settings.working_directory.resolve()
        matching_entries: list[dict[str, Any]] = []
        for entry in result["data"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("cwd"), str):
                raise AppServerError("Codex App Server returned malformed skills/list entries")
            try:
                entry_cwd = Path(entry["cwd"]).resolve()
            except OSError as error:
                raise AppServerError("Codex App Server returned an invalid skills/list working directory") from error
            if entry_cwd == expected_cwd:
                matching_entries.append(entry)

        if len(matching_entries) != 1:
            raise AppServerError("Codex App Server did not return exactly one skills/list entry for the configured working directory")
        entry = matching_entries[0]
        errors = entry.get("errors")
        skills = entry.get("skills")
        if not isinstance(errors, list) or errors:
            raise AppServerError("Codex App Server could not completely discover skills for the configured working directory")
        if not isinstance(skills, list):
            raise AppServerError("Codex App Server returned malformed skills/list skills")

        paths: set[str] = set()
        disabled: list[dict[str, Any]] = []
        for skill in skills:
            if not isinstance(skill, dict) or not isinstance(skill.get("path"), str) or not skill["path"]:
                raise AppServerError("Codex App Server returned a skill without a valid path")
            path = skill["path"]
            if path not in paths:
                paths.add(path)
                disabled.append({"path": path, "enabled": False})
        return {"skills": {"config": disabled}}


def _agent_message_texts(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [
        item["text"]
        for item in items
        if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str)
    ]


def _safe_log_value(value: Any) -> str:
    """Keep child diagnostics bounded and single-line; never log request bodies."""
    return str(value).replace("\n", " ").replace("\r", " ").strip()[-1000:]


def _lean_profile_contents(instructions_path: Path) -> str:
    """Return the complete, project-managed text-only Codex profile."""
    model_catalog = Path(__file__).with_name("codex-api-model-catalog.json").resolve()
    return f'''# Managed by codex-api.py. Changes are reconciled at proxy startup.
# This dedicated profile does not affect normal interactive Codex sessions.

# Replace the built-in agent instructions with the proxy's text-only instructions.
model_instructions_file = {json.dumps(str(instructions_path))}

# Project-owned catalog that removes remaining model-metadata tools for the proxy.
model_catalog_json = {json.dumps(str(model_catalog))}

# Do not inject AGENTS.md or fallback project documents.
project_doc_max_bytes = 0

# Disable web-search context and its tool.
web_search = "disabled"

[features]
apps = false
browser_use = false
computer_use = false
goals = false
image_generation = false
in_app_browser = false
multi_agent = false
plugins = false
remote_plugin = false
shell_snapshot = false
shell_tool = false
tool_suggest = false
unified_exec = false
'''


def _codex_home() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    return Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"


def _profile_points_to(profile_path: Path, instructions_path: Path) -> bool:
    """Whether the complete managed profile matches this proxy revision."""
    try:
        with profile_path.open("rb") as profile_file:
            parsed = tomllib.load(profile_file)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    try:
        expected = tomllib.loads(_lean_profile_contents(instructions_path))
    except tomllib.TOMLDecodeError:  # pragma: no cover - static template guard
        return False
    return parsed == expected


def _ensure_lean_profile(instructions_path: Path) -> Path:
    """Create or repair the proxy's fixed Codex profile before each startup."""
    instructions_path = instructions_path.expanduser().resolve()
    if not instructions_path.is_file():
        raise AppServerUnavailable(
            f"Configured profile instructions file does not exist: {instructions_path}"
        )

    profile_path = _codex_home() / f"{LEAN_PROFILE_NAME}.config.toml"
    if _profile_points_to(profile_path, instructions_path):
        return profile_path

    try:
        profile_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=profile_path.parent,
            prefix=f".{profile_path.name}.", suffix=".tmp", delete=False,
        ) as temporary_profile:
            temporary_path = Path(temporary_profile.name)
            temporary_profile.write(_lean_profile_contents(instructions_path))
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, profile_path)
    except OSError as error:
        with contextlib.suppress(UnboundLocalError, OSError):
            temporary_path.unlink()
        raise AppServerUnavailable(f"Could not configure Codex profile {profile_path}: {error}") from error

    LOG.info("Configured Codex profile %s to use %s", LEAN_PROFILE_NAME, instructions_path)
    return profile_path


def _profile_config_overrides(profile: str) -> list[tuple[str, Any]]:
    """Return leaf TOML settings for a named Codex profile.

    App Server has no `--profile` support in CLI 0.144.6, so copying the
    profile through its supported `--config dotted.key=value` interface is the
    narrow, process-local equivalent.  Nested tables such as `[features]` and
    `[mcp_servers.openaiDeveloperDocs]` are represented by dotted paths.
    """
    codex_home = _codex_home()
    profile_path = codex_home / f"{profile}.config.toml"
    try:
        with profile_path.open("rb") as profile_file:
            parsed = tomllib.load(profile_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AppServerUnavailable(f"Could not load Codex profile {profile_path}: {error}") from error
    if not isinstance(parsed, dict):
        raise AppServerUnavailable(f"Codex profile {profile_path} is not a TOML table")

    leaves: list[tuple[str, Any]] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if not isinstance(child_key, str):
                    raise AppServerUnavailable(f"Codex profile {profile_path} has a non-string key")
                quoted_key = _toml_key(child_key)
                walk(f"{prefix}.{quoted_key}" if prefix else quoted_key, child_value)
        elif value is not None:
            leaves.append((prefix, value))
        else:
            raise AppServerUnavailable(f"Codex profile {profile_path} contains unsupported null setting {prefix}")

    walk("", parsed)
    return leaves


def _toml_key(key: str) -> str:
    """Serialize one TOML dotted-key segment without treating a server name as syntax."""
    if _TOML_BARE_KEY.fullmatch(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _mcp_servers_disabled_override(names: list[str]) -> str:
    """Make one merge-safe TOML table override disabling the supplied names.

    The installed CLI accepts a normal dotted leaf for bare names but does not
    parse a quoted dotted-key segment.  An inline table is both valid TOML for
    arbitrary server names and merges its entry with the inherited transport
    settings, as verified with CLI 0.144.6.
    """
    return "mcp_servers={" + ",".join(
        f"{_toml_key(name)}={{enabled=false}}" for name in names
    ) + "}"


def _toml_mcp_server_names(path: Path, *, required: bool) -> set[str]:
    """Read MCP table keys only, never values that might contain credentials."""
    try:
        with path.open("rb") as config_file:
            parsed = tomllib.load(config_file)
    except FileNotFoundError:
        if required:
            raise AppServerUnavailable(f"Could not load Codex configuration {path}: file is missing")
        return set()
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AppServerUnavailable(f"Could not load Codex configuration {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise AppServerUnavailable(f"Codex configuration {path} is not a TOML table")
    servers = parsed.get("mcp_servers")
    if servers is None:
        return set()
    if not isinstance(servers, dict):
        raise AppServerUnavailable(f"Codex configuration {path} has an invalid mcp_servers table")
    names: set[str] = set()
    for name, server in servers.items():
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            raise AppServerUnavailable(f"Codex configuration {path} has an invalid MCP server entry")
        names.add(name)
    return names


def _codex_project_config_paths(working_directory: Path) -> list[Path]:
    """Return all project config layers that Codex can resolve for this CWD."""
    try:
        cwd = working_directory.resolve(strict=True)
    except OSError as error:
        raise AppServerUnavailable(f"Could not resolve Codex working directory {working_directory}: {error}") from error
    project_root = cwd
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            project_root = candidate
            break
    layers = [cwd]
    while layers[-1] != project_root:
        layers.append(layers[-1].parent)
    return [parent / ".codex" / "config.toml" for parent in reversed(layers)]


def _configured_mcp_server_names(working_directory: Path, profile: str) -> list[str]:
    """Find every MCP server that can be inherited by this App Server process.

    This intentionally collects a safe superset of trusted project layers.
    Disabling an entry that Codex would later ignore is harmless, while missing
    one would allow a server to start before a thread can be configured.
    """
    paths = [_codex_home() / "config.toml", Path("/etc/codex/config.toml")]
    paths.extend(_codex_project_config_paths(working_directory))
    names: set[str] = set()
    for path in paths:
        names.update(_toml_mcp_server_names(path, required=False))

    profile_path = _codex_home() / f"{profile}.config.toml"
    names.update(_toml_mcp_server_names(profile_path, required=True))
    return sorted(names)


def _toml_literal(value: Any) -> str:
    """Serialize profile scalar/array values for Codex's TOML `--config` parser."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    raise AppServerUnavailable(f"Unsupported value in Codex profile: {type(value).__name__}")


class CodexApi:
    def __init__(self, settings: Settings, app_server: AppServerClient | None = None) -> None:
        self.settings = settings
        self.capacity = asyncio.Semaphore(settings.max_concurrent_requests)
        self.response_state = ResponseState(settings.state_file)
        self.thread_locks: dict[str, asyncio.Lock] = {}
        self.app_server = app_server or AppServerClient(settings)

    async def start(self) -> None:
        await self.app_server.start()

    async def stop(self) -> None:
        await self.app_server.stop()

    async def run_codex(
        self, prompt: str, *, thread_id: str | None = None, ephemeral: bool = True
    ) -> TurnResult:
        """Compatibility-named bridge to the persistent App Server."""
        return await self.app_server.run_turn(prompt, thread_id=thread_id, ephemeral=ephemeral)

    async def __call__(self, scope: dict[str, Any], receive: AsgiReceive, send: AsgiSend) -> None:
        if scope["type"] == "lifespan":
            await self.handle_lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        method = scope["method"]
        path = scope["path"].rstrip("/") or "/"

        if method == "GET" and path == "/health":
            if self.app_server.healthy:
                await send_json(send, 200, {"status": "ok", "backend": "ready"})
            else:
                await send_json(
                    send,
                    503,
                    {"status": "unavailable", "backend": "unavailable", "detail": self.app_server.failure},
            )
            return
        if path.startswith("/v1/") and not authorized(
            scope,
            self.settings.bearer_tokens or ({self.settings.bearer_token} if self.settings.bearer_token else set()),
        ):
            await send_json(send, 401, error_body("Invalid API key", "authentication_error"))
            return
        if method == "GET" and path == "/v1/models":
            model = self.settings.model or "codex-configured-model"
            await send_json(
                send,
                200,
                {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "codex-cli"}]},
            )
            return
        if method == "POST" and path == "/v1/chat/completions":
            await self.handle_chat_completions(receive, send)
            return
        if method == "POST" and path == "/v1/responses":
            await self.handle_responses(receive, send)
            return
        await send_json(send, 404, error_body("Not found"))

    async def handle_lifespan(self, receive: AsgiReceive, send: AsgiSend) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.start()
                except Exception as error:
                    LOG.error("Codex App Server startup failed: %s", error)
                    await send({"type": "lifespan.startup.failed", "message": str(error)})
                else:
                    await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.stop()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def handle_chat_completions(self, receive: AsgiReceive, send: AsgiSend) -> None:
        try:
            raw_body = await read_body(receive)
            request = json.loads(raw_body)
            if not isinstance(request, dict):
                raise ValueError("request body must be a JSON object")
            if request.get("tools"):
                LOG.info("Ignoring caller tool definitions; this bridge returns text only")
            if request.get("n", 1) != 1:
                raise ValueError("only `n: 1` is supported")
            prompt = prompt_from_messages(request.get("messages"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            await send_json(send, 400, error_body(str(error)))
            return
        except RequestTooLarge as error:
            await send_json(send, 413, error_body(str(error)))
            return

        requested_model = str(request.get("model") or self.settings.model or "codex-configured-model")
        try:
            async with self.capacity:
                result = await self.run_codex(prompt)
        except AppServerError as error:
            LOG.error("Request failed: %s", error)
            await send_json(send, 502, error_body(str(error), "api_error"))
            return

        body = completion_body(result.text, requested_model, normalized_usage(result.usage))
        if request.get("stream") is True:
            await send_chat_stream(send, body)
        else:
            await send_json(send, 200, body)

    async def handle_responses(self, receive: AsgiReceive, send: AsgiSend) -> None:
        try:
            raw_body = await read_body(receive)
            request = json.loads(raw_body)
            if not isinstance(request, dict):
                raise ValueError("request body must be a JSON object")
            if request.get("background") is True:
                raise ValueError("`background` is not supported")
            requested_compaction_threshold = compaction_threshold(request.get("context_management"))
            if request.get("conversation") is not None:
                raise ValueError("`conversation` is not supported; use `previous_response_id`")
            if request.get("tools"):
                LOG.info("Ignoring caller tool definitions; this bridge returns text only")
            previous_response_id = request.get("previous_response_id")
            if previous_response_id is not None and not isinstance(previous_response_id, str):
                raise ValueError("`previous_response_id` must be a string or null")
            prompt = prompt_from_responses_request(request)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            await send_json(send, 400, error_body(str(error)))
            return
        except RequestTooLarge as error:
            await send_json(send, 413, error_body(str(error)))
            return

        thread_id = None
        previous_cumulative_usage = None
        previous_context_input_tokens = None
        if previous_response_id:
            thread_id = self.response_state.thread_for(previous_response_id)
            if thread_id is None:
                await send_json(
                    send,
                    400,
                    error_body(
                        f"Previous response with id {previous_response_id!r} was not found.",
                        param="previous_response_id",
                        code="previous_response_not_found",
                    ),
                )
                return
            previous_cumulative_usage = self.response_state.cumulative_usage_for(previous_response_id)
            previous_context_input_tokens = self.response_state.context_input_tokens_for(
                previous_response_id
            )

        should_store = request.get("store") is not False
        requested_model = str(request.get("model") or self.settings.model or "codex-configured-model")
        active_context_input_tokens: int | None = None
        try:
            async with self.capacity:
                if thread_id:
                    thread_lock = self.thread_locks.setdefault(thread_id, asyncio.Lock())
                    async with thread_lock:
                        compaction: CompactionResult | None = None
                        # The threshold applies to the active rendered context,
                        # not lifetime thread usage.  Legacy records lack that
                        # measurement, so they safely skip this one pre-turn
                        # compaction and gain a measurement after the next turn.
                        if (
                            requested_compaction_threshold is not None
                            and previous_context_input_tokens is not None
                            and previous_context_input_tokens >= requested_compaction_threshold
                        ):
                            compaction = await self.app_server.compact_thread(thread_id)
                        result = await self.run_codex(
                            prompt,
                            thread_id=thread_id,
                            ephemeral=not should_store,
                        )
                        active_context_input_tokens = usage_counter(result.usage, "input_tokens")
                        if compaction is not None:
                            result = TurnResult(
                                result.text,
                                combined_usage(compaction.usage, result.usage),
                                result.cumulative_usage,
                                result.thread_id,
                            )
                else:
                    result = await self.run_codex(
                        prompt,
                        ephemeral=not should_store,
                    )
                    active_context_input_tokens = usage_counter(result.usage, "input_tokens")
        except AppServerError as error:
            LOG.error("Responses request failed: %s", error)
            await send_json(send, 502, error_body(str(error), "api_error"))
            return

        response_id = f"resp_{uuid.uuid4().hex}"
        # App Server's `tokenUsage.last` is already the incremental turn usage.
        # Keep the cumulative snapshot only for compatibility with legacy state
        # and for clients that later switch back to the old exec backend.
        turn_usage = result.usage
        if should_store:
            try:
                self.response_state.remember(
                    response_id,
                    result.thread_id,
                    result.cumulative_usage,
                    active_context_input_tokens,
                )
            except OSError as error:
                LOG.error("Could not persist response state: %s", error)
                await send_json(
                    send,
                    500,
                    error_body("Could not persist response state", "api_error", code="server_error"),
                )
                return

        body = response_body(result.text, requested_model, normalized_usage(turn_usage), response_id, request)
        if request.get("stream") is True:
            await send_responses_stream(send, body)
        else:
            await send_json(send, 200, body)


class RequestTooLarge(Exception):
    pass


def parse_codex_events(stdout: bytes) -> tuple[str, dict[str, Any] | None, str | None]:
    """Extract the final message, usage, and thread ID from Codex JSONL stdout."""
    response_text = ""
    usage: dict[str, Any] | None = None
    thread_id: str | None = None
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            LOG.warning("Ignoring non-JSON Codex stdout line: %s", line[:200])
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    response_text = text
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
    return response_text.strip(), usage, thread_id


async def read_body(receive: AsgiReceive) -> str:
    parts: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        part = message.get("body", b"")
        size += len(part)
        if size > MAX_REQUEST_BYTES:
            raise RequestTooLarge(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        parts.append(part)
        if not message.get("more_body", False):
            break
    return b"".join(parts).decode("utf-8")


def authorized(scope: dict[str, Any], tokens: Collection[str]) -> bool:
    """Accept an OpenAI-compatible ``Authorization: Bearer <API key>`` header."""
    if not tokens:
        return True
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    supplied = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
    return any(hmac.compare_digest(supplied, f"Bearer {token}") for token in tokens)


async def send_json(send: AsgiSend, status: int, payload: Any) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def send_chat_stream(send: AsgiSend, completion: dict[str, Any]) -> None:
    """Emit a valid one-chunk stream after Codex finishes generating."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
            ],
        }
    )
    base = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": completion["choices"][0]["message"]["content"]},
                    "finish_reason": None,
                }
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    for chunk in chunks:
        data = f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
        await send({"type": "http.response.body", "body": data, "more_body": True})
    await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})


def responses_sse_event(event_name: str, sequence_number: int, payload: dict[str, Any]) -> bytes:
    data = json.dumps({"type": event_name, "sequence_number": sequence_number, **payload})
    return f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")


async def send_responses_stream(send: AsgiSend, response: dict[str, Any]) -> None:
    """Emit a buffered but protocol-shaped Responses API event stream."""
    message = response["output"][0]
    content = message["content"][0]
    output_text = content["text"]
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
            ],
        }
    )
    in_progress_response = {**response, "status": "in_progress", "output": []}
    added_message = {**message, "status": "in_progress", "content": []}
    events = [
        ("response.created", {"response": in_progress_response}),
        ("response.in_progress", {"response": in_progress_response}),
        ("response.output_item.added", {"output_index": 0, "item": added_message}),
        (
            "response.content_part.added",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": [], "logprobs": []},
            },
        ),
        (
            "response.output_text.delta",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": output_text,
                "logprobs": [],
            },
        ),
        (
            "response.output_text.done",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "text": output_text,
                "logprobs": [],
            },
        ),
        (
            "response.content_part.done",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "part": content,
            },
        ),
        ("response.output_item.done", {"output_index": 0, "item": message}),
        ("response.completed", {"response": response}),
    ]
    for sequence_number, (event_name, payload) in enumerate(events):
        await send(
            {
                "type": "http.response.body",
                "body": responses_sse_event(event_name, sequence_number, payload),
                "more_body": True,
            }
        )
    await send({"type": "http.response.body", "body": b""})


def load_config(path: Path, parser: argparse.ArgumentParser) -> dict[str, Any]:
    """Read the optional-on-purpose, human-editable YAML configuration."""
    try:
        with path.open(encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
    except FileNotFoundError:
        parser.error(f"configuration file does not exist: {path}")
    except yaml.YAMLError as error:
        parser.error(f"could not parse configuration file {path}: {error}")

    if config is None:
        return {}
    if not isinstance(config, dict):
        parser.error(f"configuration file must contain a YAML mapping: {path}")
    return config


def configured_value(
    cli_value: Any,
    config: dict[str, Any],
    config_key: str,
    environment_key: str,
    fallback: Any,
) -> Any:
    """Apply the precedence order: command line, environment, YAML, fallback."""
    if cli_value is not None:
        return cli_value
    if environment_key in os.environ:
        return os.environ[environment_key]
    return config.get(config_key, fallback)


def configured_bearer_tokens(
    config: dict[str, Any], parser: argparse.ArgumentParser, legacy_token: str | None
) -> frozenset[str]:
    """Read the optional YAML token allow-list and reject unsafe/mistyped entries."""
    value = config.get("bearer_tokens", [])
    if value is None:
        value = []
    if not isinstance(value, list) or any(not isinstance(token, str) or not token.strip() for token in value):
        parser.error("bearer_tokens must be a YAML list of non-empty strings")
    return frozenset([*value, *([legacy_token] if legacy_token else [])])


def parse_args() -> Settings:
    parser = argparse.ArgumentParser(description=__doc__)
    default_config = Path(__file__).with_name("config.yaml")
    parser.add_argument("--config", type=Path, default=default_config, help="YAML settings file")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--codex")
    parser.add_argument("--working-directory", type=Path)
    parser.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write"),
    )
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--model")
    parser.add_argument("--thinking-effort", choices=("minimal", "low", "medium", "high", "xhigh"))
    parser.add_argument("--bearer-token")
    parser.add_argument("--max-concurrent-requests", type=int)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--app-server-start-timeout", type=float)
    parser.add_argument("--log-level")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path, parser)

    host = configured_value(args.host, config, "host", "CODEX_API_HOST", "127.0.0.1")
    port = configured_value(args.port, config, "port_number", "CODEX_API_PORT", 8000)
    codex_binary = configured_value(args.codex, config, "codex_binary", "CODEX_API_BINARY", "codex")
    working_directory_value = configured_value(
        args.working_directory, config, "working_directory", "CODEX_API_WORKDIR", os.getcwd()
    )
    sandbox = configured_value(args.sandbox, config, "sandbox", "CODEX_API_SANDBOX", "read-only")
    timeout_seconds = configured_value(args.timeout, config, "timeout_seconds", "CODEX_API_TIMEOUT", 300)
    model = configured_value(args.model, config, "model", "CODEX_API_MODEL", "gpt-5.6-terra")
    thinking_effort = configured_value(
        args.thinking_effort, config, "thinking_effort", "CODEX_API_THINKING_EFFORT", "medium"
    )
    profile_instructions_value = config.get("profile_instructions")
    bearer_token = configured_value(args.bearer_token, config, "bearer_token", "CODEX_API_TOKEN", None)
    bearer_token = str(bearer_token) if bearer_token else None
    bearer_tokens = configured_bearer_tokens(config, parser, bearer_token)
    max_concurrent_requests = configured_value(
        args.max_concurrent_requests, config, "max_concurrent_requests", "CODEX_API_MAX_CONCURRENT", 2
    )
    state_file_value = configured_value(
        args.state_file, config, "state_file", "CODEX_API_STATE_FILE", "codex-api-state.json"
    )
    app_server_start_timeout = configured_value(
        args.app_server_start_timeout,
        config,
        "app_server_start_timeout_seconds",
        "CODEX_API_APP_SERVER_START_TIMEOUT",
        30,
    )
    log_level = configured_value(args.log_level, config, "log_level", "CODEX_API_LOG_LEVEL", "info")

    try:
        port = int(port)
        timeout_seconds = float(timeout_seconds)
        max_concurrent_requests = int(max_concurrent_requests)
        app_server_start_timeout = float(app_server_start_timeout)
    except (TypeError, ValueError) as error:
        parser.error(f"configuration contains an invalid numeric value: {error}")
    if sandbox not in {"read-only", "workspace-write"}:
        parser.error("sandbox must be `read-only` or `workspace-write`")
    if thinking_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        parser.error("thinking_effort must be minimal, low, medium, high, or xhigh")
    if not isinstance(log_level, str) or log_level.lower() not in {"critical", "error", "warning", "info", "debug"}:
        parser.error("log_level must be critical, error, warning, info, or debug")
    if not isinstance(profile_instructions_value, str) or not profile_instructions_value.strip():
        parser.error("profile_instructions must be a non-empty path to an instructions file")

    working_directory = Path(working_directory_value).expanduser().resolve()
    profile_instructions = Path(profile_instructions_value).expanduser()
    if not profile_instructions.is_absolute():
        profile_instructions = (config_path.parent / profile_instructions).resolve()
    state_file = Path(state_file_value).expanduser()
    if not state_file.is_absolute():
        state_file = (config_path.parent / state_file).resolve()
    if not working_directory.is_dir():
        parser.error(f"working directory does not exist: {working_directory}")
    if not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")
    if timeout_seconds <= 0:
        parser.error("timeout must be positive")
    if max_concurrent_requests < 1:
        parser.error("--max-concurrent-requests must be at least 1")
    if app_server_start_timeout <= 0:
        parser.error("--app-server-start-timeout must be positive")
    return Settings(
        host=str(host),
        port=port,
        codex_binary=str(codex_binary),
        working_directory=working_directory,
        sandbox=sandbox,
        timeout_seconds=timeout_seconds,
        model=str(model) if model else None,
        thinking_effort=thinking_effort,
        profile_instructions=profile_instructions,
        bearer_token=bearer_token,
        max_concurrent_requests=max_concurrent_requests,
        state_file=state_file,
        app_server_start_timeout=app_server_start_timeout,
        log_level=log_level.lower(),
        bearer_tokens=bearer_tokens,
    )


def main() -> int:
    settings = parse_args()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    if settings.host not in {"127.0.0.1", "localhost", "::1"} and not (
        settings.bearer_token or settings.bearer_tokens
    ):
        LOG.warning("Listening beyond localhost without CODEX_API_TOKEN authentication")
    uvicorn.run(CodexApi(settings), host=settings.host, port=settings.port, log_level=settings.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
