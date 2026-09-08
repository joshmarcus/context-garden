"""Serve and replay CG-356 onboarding recovery against a disposable garden."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from garden.onboard import onboard_project
from garden.scaffold import init_garden
from garden.store import Store


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=CG-356 replay", "-c", "user.email=cg356@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _project(root: Path) -> Path:
    repo = root / "sample-web"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _write(repo / "README.md", "# Sample web\n")
    _write(repo / "package.json", json.dumps({"scripts": {"test": "node --test", "lint": "eslint ."}}))
    _write(repo / "package-lock.json", "{}")
    _write(repo / "src" / "index.js", "// TODO: add a health endpoint\n")
    _write(repo / "TODO.md", "# Roadmap\n\n- Add structured logging\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


def _valid_plan(_store: Store, _prompt: str) -> str:
    return json.dumps([{
        "title": "Add health endpoint", "priority": 1, "estimate": "S", "difficulty": "easy",
        "depends_on": [], "reading": [], "discovered_from": "onboard:src/index.js",
        "body": "## Goal\n\nAdd health output.\n\n## Acceptance criteria\n\n- [ ] A test covers the endpoint.\n",
    }])


def _rejected_plan(store: Store, prompt: str) -> str:
    item = json.loads(_valid_plan(store, prompt))[0]
    item.update({"title": "Replace the database engine", "body": "## Goal\n\nMigrate all persistence.\n"})
    item.pop("discovered_from")
    return json.dumps([item])


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class Replay:
    def __init__(self, root: Path) -> None:
        self.repo = _project(root)
        self.garden = root / "garden"
        init_garden(self.garden, "existing")
        config_path = self.garden / "garden.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["custom_setting"] = "preserve me"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False))
        _write(self.garden / "notes.md", "Existing owner note.\n")
        self.before = _file_hashes(self.garden)
        self.requests: list[dict[str, object]] = []

    def state(self) -> dict[str, object]:
        product = self.garden / "sample-web"
        tasks = []
        if product.exists():
            tasks = [task.status.value for task in Store(self.garden).product("sample-web").phases[0].tasks]
        return {"product_exists": product.exists(), "task_statuses": tasks}

    def reject(self) -> tuple[int, dict[str, object]]:
        try:
            onboard_project(self.repo, self.garden, planner=_rejected_plan)
        except ValueError as error:
            unchanged = _file_hashes(self.garden) == self.before
            return 422, {"error": str(error), "preexisting_files_unchanged": unchanged, **self.state()}
        raise AssertionError("rejected planner output unexpectedly succeeded")

    def retry(self) -> tuple[int, dict[str, object]]:
        created = onboard_project(self.repo, self.garden, planner=_valid_plan)
        root = self.garden.resolve()
        return 201, {"created": sorted(str(path.relative_to(root)) for path in created), **self.state()}

    def collision(self) -> tuple[int, dict[str, object]]:
        before = _file_hashes(self.garden)
        try:
            onboard_project(self.repo, self.garden, planner=_valid_plan)
        except ValueError as error:
            return 409, {"error": str(error), "files_unchanged": _file_hashes(self.garden) == before, **self.state()}
        raise AssertionError("existing product collision unexpectedly succeeded")


def _serve(replay: Replay) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def _respond(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/onboarding":
                self._respond(404, {"error": "not found"})
                return
            self._respond(200, replay.state())

        def do_POST(self) -> None:  # noqa: N802
            action = {"/onboarding/reject": replay.reject, "/onboarding/retry": replay.retry,
                      "/onboarding/collision": replay.collision}.get(self.path)
            if action is None:
                self._respond(404, {"error": "not found"})
                return
            status, body = action()
            self._respond(status, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _request(base_url: str, method: str, path: str, replay: Replay) -> dict[str, object]:
    request = Request(base_url + path, method=method)
    try:
        with urlopen(request) as response:  # noqa: S310 - loopback disposable server
            status, body = response.status, json.loads(response.read())
    except Exception as error:
        status = getattr(error, "code", 0)
        body = json.loads(error.read()) if hasattr(error, "read") else {"error": str(error)}
    event = {"method": method, "path": path, "status_code": status, "observation": body}
    replay.requests.append(event)
    return event


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if actual_head != args.head:
        parser.error("replay head differs from the checkout")
    with tempfile.TemporaryDirectory(prefix="cg356-replay-") as temporary:
        replay = Replay(Path(temporary))
        server = _serve(replay)
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            empty = _request(base_url, "GET", "/onboarding", replay)
            failed = _request(base_url, "POST", "/onboarding/reject", replay)
            recovered = _request(base_url, "POST", "/onboarding/retry", replay)
            collision = _request(base_url, "POST", "/onboarding/collision", replay)
        finally:
            server.shutdown()
            server.server_close()
    manifest = {
        "producer": "garden.cg356.onboarding-replay/v1", "head": args.head,
        "started_at": datetime.now(UTC).isoformat(), "environment": "disposable-served-http",
        "status": "pass",
        "requests": replay.requests,
        "states": {
            "empty": {"status": "pass", "action": "GET /onboarding", "observed": empty["observation"]},
            "failure": {"status": "pass", "action": "POST /onboarding/reject", "observed": failed["observation"]},
            "recovery": {"status": "pass", "action": "POST /onboarding/retry", "observed": recovered["observation"]},
            "collision": {"status": "pass", "action": "POST /onboarding/collision", "observed": collision["observation"]},
        },
    }
    assert empty["status_code"] == 200 and empty["observation"] == {"product_exists": False, "task_statuses": []}
    assert failed["status_code"] == 422 and failed["observation"]["preexisting_files_unchanged"] is True
    assert failed["observation"]["product_exists"] is False
    assert recovered["status_code"] == 201 and recovered["observation"]["task_statuses"] == ["draft"]
    assert collision["status_code"] == 409 and collision["observation"]["files_unchanged"] is True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
