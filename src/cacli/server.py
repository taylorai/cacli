"""Lightweight HTTP server for the cacli dashboard."""

import json
import mimetypes
import subprocess
from dataclasses import asdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from cacli.sessions import (
    delete_session,
    list_sessions,
    load_session,
    sync_session_status,
)

SESSIONS_DIR = Path.home() / ".cacli" / "sessions"
DASHBOARD_DIR = Path(__file__).parent / "dashboard_dist"


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves the dashboard static files and API endpoints."""

    def log_message(self, format, *args):
        pass  # Silence request logs

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, content_type="text/plain"):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _parse_path(self):
        """Parse the URL path, stripping query string."""
        return self.path.split("?")[0]

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self._parse_path()

        if path == "/api/sessions":
            return self._handle_list_sessions()
        if path.startswith("/api/sessions/") and path.endswith("/log"):
            session_id = path.split("/")[3]
            return self._handle_get_log(session_id)
        if path.startswith("/api/sessions/"):
            session_id = path.split("/")[3]
            return self._handle_get_session(session_id)

        # Serve static files
        self._serve_static(path)

    def do_POST(self):
        path = self._parse_path()
        if path.startswith("/api/sessions/") and path.endswith("/kill"):
            session_id = path.split("/")[3]
            return self._handle_kill_session(session_id)
        self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        path = self._parse_path()
        if path.startswith("/api/sessions/"):
            session_id = path.split("/")[3]
            return self._handle_delete_session(session_id)
        self._send_json({"error": "Not found"}, 404)

    def _handle_list_sessions(self):
        sessions = list_sessions()
        sessions = [sync_session_status(s) for s in sessions]
        self._send_json([asdict(s) for s in sessions])

    def _handle_get_session(self, session_id):
        session = load_session(session_id)
        if not session:
            return self._send_json({"error": "Session not found"}, 404)
        session = sync_session_status(session)
        self._send_json(asdict(session))

    def _handle_get_log(self, session_id):
        log_path = SESSIONS_DIR / f"{session_id}.log"
        if not log_path.exists():
            return self._send_text("", 404)
        self._send_text(log_path.read_text(errors="replace"))

    def _handle_kill_session(self, session_id):
        session = load_session(session_id)
        if not session:
            return self._send_json({"error": "Session not found"}, 404)
        if session.status != "running":
            return self._send_json({"error": "Session is not running"}, 400)
        subprocess.run(
            ["tmux", "kill-session", "-t", session.tmux_session],
            capture_output=True,
        )
        session = sync_session_status(session)
        self._send_json(asdict(session))

    def _handle_delete_session(self, session_id):
        session = load_session(session_id)
        if not session:
            return self._send_json({"error": "Session not found"}, 404)
        delete_session(session_id)
        self._send_json({"ok": True})

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"

        file_path = DASHBOARD_DIR / path.lstrip("/")
        if file_path.is_file():
            content_type, _ = mimetypes.guess_type(str(file_path))
            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            # SPA fallback: serve index.html for any unmatched route
            index = DASHBOARD_DIR / "index.html"
            if index.is_file():
                body = index.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_text(
                    "Dashboard not built. Run: cd dashboard && npm run build", 404
                )


def run_server(port=8420):
    """Start the dashboard server."""
    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"cacli dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
