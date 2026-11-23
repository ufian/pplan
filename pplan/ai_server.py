"""Private Compose service hosting Codex CLI, with no access to plan storage."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os

from pplan.ai import CodexInterpreter
from pplan.core import PlanError


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/interpret":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                raise ValueError()
            payload = json.loads(self.rfile.read(length))
            message, context = payload["message"], payload["context"]
            if not isinstance(message, str) or not 0 < len(message) <= 2000 or not isinstance(context, dict):
                raise ValueError()
            result = self.server.interpreter.interpret(message, context)
            body = json.dumps(result).encode()
            self.send_response(200)
        except (KeyError, TypeError, ValueError, PlanError):
            body = b'{"error":"Interpretation unavailable; no changes applied."}'
            self.send_response(422)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(("0.0.0.0", 8080), Handler)
    server.interpreter = CodexInterpreter(model=os.environ.get("CODEX_MODEL", "gpt-6-luna"))
    server.serve_forever()


if __name__ == "__main__":
    main()
