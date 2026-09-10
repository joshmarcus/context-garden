"""Run Codex through a local response-accounting proxy for OpenRouter.

Codex remains responsible for the agent and tool loop.  The proxy observes the provider's
OpenAI-compatible response usage and appends one normal Codex ``turn.completed`` event with
the run totals.  This avoids estimating a multi-provider charge from a local price table.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx


def _provider_key(name: str) -> str:
    """Consume the runner's one-shot credential pipe, or use env for direct invocation."""
    raw_fd = os.environ.pop("GARDEN_HARNESS_API_KEY_FD", "")
    transported_name = os.environ.pop("GARDEN_HARNESS_API_KEY_NAME", "")
    environment_value = os.environ.pop(name, "")
    if raw_fd:
        if transported_name != name:
            raise ValueError("OpenRouter credential transport name does not match harness configuration")
        fd = int(raw_fd)
        try:
            return os.read(fd, 1024 * 1024).decode().rstrip("\n")
        finally:
            os.close(fd)
    return environment_value


def _response_objects(body: bytes) -> list[dict[str, Any]]:
    """Decode JSON or SSE response objects, ignoring non-JSON stream sentinels."""
    text = body.decode("utf-8", "replace")
    candidates = [text] if not text.lstrip().startswith("data:") else [
        line[5:].strip() for line in text.splitlines() if line.startswith("data:")
    ]
    objects: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _usage_from(value: dict[str, Any]) -> dict[str, int | float] | None:
    """Find the Responses usage object in a response or response.completed event."""
    response = value.get("response")
    if isinstance(response, dict):
        value = response
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return None
    result: dict[str, int | float] = {}
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cost"):
        item = usage.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = item
    details = usage.get("input_tokens_details")
    if isinstance(details, dict) and "cached_input_tokens" not in result:
        cached = details.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            result["cached_input_tokens"] = cached
    return result or None


class UsageTotals:
    def __init__(self) -> None:
        self.values: dict[str, int | float] = {}
        self._lock = threading.Lock()

    def observe(self, body: bytes) -> None:
        # Streaming responses can repeat usage in intermediate events. The last usage object
        # is the authoritative total for this request and is added once.
        usages = [usage for obj in _response_objects(body) if (usage := _usage_from(obj))]
        if not usages:
            return
        with self._lock:
            for key, value in usages[-1].items():
                self.values[key] = self.values.get(key, 0) + value


class TurnLimit:
    """Reserve at most ``maximum`` provider Responses calls for one adapter run."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.count = 0
        self.exceeded = threading.Event()
        self._lock = threading.Lock()

    def reserve(self) -> bool:
        with self._lock:
            if self.maximum > 0 and self.count >= self.maximum:
                self.exceeded.set()
                return False
            self.count += 1
            return True


def _handler(upstream: str, api_key: str, totals: UsageTotals,
             turns: TurnLimit) -> type[BaseHTTPRequestHandler]:
    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/responses") and not turns.reserve():
                body = json.dumps({
                    "error": {
                        "message": f"OpenRouter run exceeded max_turns={turns.maximum}",
                        "type": "garden_max_turns",
                    }
                }).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            length = int(self.headers.get("Content-Length", "0"))
            headers = {
                key: value for key, value in self.headers.items()
                if key.lower() not in {"host", "content-length", "authorization", "connection"}
            }
            headers["Authorization"] = f"Bearer {api_key}"
            try:
                response = httpx.post(
                    f"{upstream.rstrip('/')}{self.path}", content=self.rfile.read(length),
                    headers=headers, timeout=None,
                )
            except httpx.HTTPError as exc:
                self.send_error(502, str(exc))
                return
            totals.observe(response.content)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ProxyHandler


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-env", required=True)
    parser.add_argument("--max-turns", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    api_key = _provider_key(args.api_key_env)
    if not api_key:
        parser.error(f"{args.api_key_env} is not set")
    totals = UsageTotals()
    turns = TurnLimit(max(args.max_turns, 0))
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), _handler(args.base_url, api_key, totals, turns)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    local_url = f"http://127.0.0.1:{server.server_port}"
    command = [
        f'model_providers.openrouter.base_url="{local_url}"'
        if item.startswith("model_providers.openrouter.base_url=") else item
        for item in command
    ]
    try:
        child_env = {**os.environ, args.api_key_env: "garden-local-openrouter-proxy"}
        child = subprocess.Popen(command, stdin=sys.stdin, env=child_env)
        while child.poll() is None:
            if turns.exceeded.wait(0.05):
                child.terminate()
                break
        return_code = child.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if turns.exceeded.is_set():
        print(f"garden_max_turns: OpenRouter run exceeded max_turns={turns.maximum}",
              file=sys.stderr, flush=True)
    if totals.values:
        print(json.dumps({"type": "turn.completed", "usage": totals.values}), flush=True)
    return return_code or (1 if turns.exceeded.is_set() else 0)


if __name__ == "__main__":
    raise SystemExit(run())
