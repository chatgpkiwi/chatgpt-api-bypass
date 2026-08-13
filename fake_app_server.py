#!/usr/bin/env python3
"""Deterministic stdio JSON-RPC fixture for App Server transport tests."""

import json
import os
import sys
import time
from pathlib import Path


next_thread = 0


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def record_request(method, params):
    """Optionally record sanitized deterministic-fixture requests for tests."""
    log_path = os.environ.get("FAKE_APP_SERVER_REQUEST_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps({"method": method, "params": params}) + "\n")


def record_argv():
    """Record fixture argv for process-local config assertions in tests."""
    log_path = os.environ.get("FAKE_APP_SERVER_REQUEST_LOG")
    if log_path:
        with Path(log_path).open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps({"method": "process/argv", "params": sys.argv[1:]}) + "\n")


def fake_skills(params):
    """Return configured skills/list data without touching real skill roots."""
    mode = os.environ.get("FAKE_APP_SERVER_SKILLS", "default")
    if mode == "error":
        return None
    if mode == "malformed":
        return {"notData": []}
    cwd = (params.get("cwds") or [os.getcwd()])[0]
    if mode == "zero":
        skills = []
    elif mode == "future":
        skills = [
            {"name": "existing", "path": "/fake/skills/existing/SKILL.md", "enabled": True},
            {"name": "future-skill", "path": "/fake/skills/future/SKILL.md", "enabled": True},
        ]
    else:
        skills = [
            {"name": "existing", "path": "/fake/skills/existing/SKILL.md", "enabled": True},
        ]
    return {"data": [{"cwd": cwd, "skills": skills, "errors": []}]}


def fake_mcp_status():
    """Return deterministic MCP inventory without starting an MCP server."""
    mode = os.environ.get("FAKE_APP_SERVER_MCP_STATUS", "zero")
    if mode == "malformed":
        return {"notData": []}
    if mode == "paged":
        return {"data": [], "nextCursor": "more"}
    if mode == "one":
        data = [{"name": "inherited", "authStatus": "unsupported", "serverInfo": {"name": "inherited", "version": "1"}, "tools": {"leak": {}}, "resources": [], "resourceTemplates": []}]
    elif mode == "several":
        data = [
            {"name": "inherited", "authStatus": "unsupported", "serverInfo": {"name": "inherited", "version": "1"}, "tools": {"leak": {}}, "resources": [], "resourceTemplates": []},
            {"name": "future", "authStatus": "unsupported", "serverInfo": {"name": "future", "version": "1"}, "tools": {}, "resources": [{"name": "leak", "uri": "fake://leak"}], "resourceTemplates": []},
        ]
    else:
        data = []
    return {"data": data, "nextCursor": None}


record_argv()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    record_request(method, params)
    if method == "initialize":
        emit({"id": request_id, "result": {"codexHome": "/tmp", "platformFamily": "unix", "platformOs": "linux", "userAgent": "fake"}})
    elif method == "skills/list":
        result = fake_skills(params)
        if result is None:
            emit({"id": request_id, "error": {"code": -32000, "message": "skill discovery failed"}})
        else:
            emit({"id": request_id, "result": result})
    elif method == "mcpServerStatus/list":
        emit({"id": request_id, "result": fake_mcp_status()})
    elif method == "thread/start":
        next_thread += 1
        thread_id = f"thread-{next_thread}"
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
        emit({"method": "thread/started", "params": {"threadId": thread_id, "thread": {"id": thread_id}}})
    elif method == "thread/resume":
        thread_id = params["threadId"]
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "thread/compact/start":
        thread_id = params["threadId"]
        turn_id = f"compact-{time.time_ns()}"
        mode = os.environ.get("FAKE_APP_SERVER_COMPACTION", "complete")
        emit({"id": request_id, "result": {}})
        emit({"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        if mode == "hang":
            continue
        if mode == "malformed":
            emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "item": {"type": "contextCompaction"}}})
            continue
        if mode == "failed":
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "failed", "items": []}}})
            continue
        usage = {"inputTokens": 7, "cachedInputTokens": 2, "outputTokens": 2, "reasoningOutputTokens": 1}
        emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 1, "item": {"type": "contextCompaction", "id": "compact-item"}}})
        emit({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {"last": usage, "total": usage}}})
        emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": []}}})
    elif method == "turn/start":
        thread_id = params["threadId"]
        turn_id = f"turn-{time.time_ns()}"
        text = params["input"][0]["text"]
        if text == "MALFORMED":
            sys.stdout.write("not json\n")
            sys.stdout.flush()
            continue
        if text == "DIE":
            sys.exit(9)
        emit({"id": request_id, "result": {"turn": {"id": turn_id, "items": []}}})
        if text == "HANG":
            continue
        if text == "SLOW":
            time.sleep(0.15)
        usage = {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 3, "reasoningOutputTokens": 1, "totalTokens": 13}
        emit({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 1, "item": {"type": "agentMessage", "id": "message", "text": f"answer:{text}"}}})
        emit({"method": "thread/tokenUsage/updated", "params": {"threadId": thread_id, "turnId": turn_id, "tokenUsage": {"last": usage, "total": usage}}})
        emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "items": []}}})
    elif method == "turn/interrupt":
        emit({"id": request_id, "result": {}})
    elif request_id is not None:
        emit({"id": request_id, "error": {"code": -32601, "message": f"unsupported {method}"}})
