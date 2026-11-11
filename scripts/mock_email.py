"""Idempotent local Resend double used only by the repeatable load test."""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class DeliveryLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts = 0
        self._accepted: dict[str, str] = {}

    def accept(self, key: str) -> tuple[str, bool]:
        with self._lock:
            self._attempts += 1
            duplicate = key in self._accepted
            message_id = self._accepted.setdefault(
                key, "mock-" + hashlib.sha256(key.encode()).hexdigest()[:20]
            )
            return message_id, duplicate

    def stats(self) -> dict[str, int]:
        with self._lock:
            unique = len(self._accepted)
            return {
                "requests": self._attempts,
                "accepted_alerts": unique,
                "deduplicated_requests": self._attempts - unique,
            }


def handler(ledger: DeliveryLedger) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._json(HTTPStatus.OK, {"ok": True})
            elif self.path == "/stats":
                self._json(HTTPStatus.OK, ledger.stats())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/emails":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            key = self.headers.get("Idempotency-Key", "").strip()
            if not key:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "missing Idempotency-Key"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(min(length, 1 << 20))
            message_id, duplicate = ledger.accept(key)
            self._json(HTTPStatus.OK, {"id": message_id, "deduplicated": duplicate})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            payload = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--addr", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    ledger = DeliveryLedger()
    server = ThreadingHTTPServer((args.addr, args.port), handler(ledger))
    print(f"mock email listening on {args.addr}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
