#!/usr/bin/env python3
"""OpenAI Responses-compatible OpenRouter stub for offline adapter tests."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/v1/responses" or self.headers.get("Authorization") != "Bearer offline-test-key":
            self.send_error(401)
            return
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        completed = {"type": "response.completed", "response": {
            "id": "resp_fake", "object": "response", "status": "completed",
            "model": request.get("model", "qwen/qwen3-coder"), "output": [],
            "usage": {"input_tokens": 120, "output_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 20}, "cost": 0.0042}}}
        response = f"data: {json.dumps(completed)}\n\ndata: [DONE]\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        return

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port-file", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(args.port_file, "w") as port_file:
        port_file.write(str(server.server_port))
    server.serve_forever()

if __name__ == "__main__":
    main()
