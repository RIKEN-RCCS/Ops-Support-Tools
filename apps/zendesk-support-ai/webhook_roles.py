"""Opt-in WSGI entry points for separately deployed webhook roles."""
import http.client
import socket
import time

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import NotFound, ServiceUnavailable

QUEUE_PATHS = frozenset(("/zendesk/webhook/triage", "/zendesk/webhook/followup"))


class QueueConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost", timeout=10)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def queue_frontend(socket_path):
    """Expose only the two queue operations. No storage or worker imports."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.before_request
    def reject_diagnostic_methods():
        if request.method in {"TRACE", "TRACK"}:
            return jsonify(ok=False, error="method_not_supported"), 501

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    def forward():
        connection = QueueConnection(socket_path)
        try:
            headers = {name: request.headers[name] for name in
                       ("Content-Type", "Authorization", "X-Support-AI-Webhook-Token")
                       if name in request.headers}
            connection.request("POST", request.path, request.get_data(), headers)
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                return jsonify(ok=False, error="queue response too large"), 502
            return Response(body, status=response.status,
                            content_type=response.getheader("Content-Type"))
        except (OSError, http.client.HTTPException):
            return jsonify(ok=False, error="queue service unavailable"), 503
        finally:
            connection.close()

    for path in QUEUE_PATHS:
        app.add_url_rule(path, path, forward, methods=["POST"])
    return app


def backend(role, *, monitor_max_age=None):
    """Restrict the internal receiver even when called directly over its socket."""
    from webhook import app

    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    if role not in {"queue", "note", "monitor"}:
        raise ValueError("unknown webhook role")

    if role == "monitor" and (not isinstance(monitor_max_age, int) or monitor_max_age <= 0):
        raise ValueError("monitor requires a positive snapshot age limit in seconds")

    def dispatch(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if role == "queue":
            allowed = path in QUEUE_PATHS
        elif role == "note":
            allowed = path == "/zendesk/webhook/send_internal_note"
        else:
            allowed = path == "/support-ai/monitor" or path.startswith("/support-ai/monitor/")
        if allowed and role == "monitor":
            import common
            try:
                age = time.time() - common.QUEUE_DB.stat().st_mtime
            except OSError:
                age = -1
            if not 0 <= age <= monitor_max_age:
                return ServiceUnavailable("Queue snapshot unavailable or stale")(environ, start_response)
        if allowed:
            return app(environ, start_response)
        return NotFound()(environ, start_response)

    return dispatch


def refresh_monitor_snapshot(source, destination, *, stop=None, timeout=20):
    """Publish a consistent read-only monitor database in an existing private directory."""
    from contextlib import closing
    from pathlib import Path
    import os
    import sqlite3
    import tempfile

    if timeout <= 0:
        raise ValueError("snapshot timeout must be positive")
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if stop is not None and stop.is_set():
            raise InterruptedError("snapshot stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("snapshot deadline exceeded")

    progress(0, 0, 0)
    target = Path(destination)
    if Path(source).resolve() == target.resolve():
        raise ValueError("monitor snapshot requires a separate destination")
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".monitor-", delete=False) as tmp:
        pending = Path(tmp.name)
    try:
        with closing(sqlite3.connect(Path(source).absolute().as_uri() + "?mode=ro", uri=True, timeout=1)) as reader:
            with closing(sqlite3.connect(pending)) as writer:
                reader.backup(writer, pages=128, progress=progress, sleep=0.05)
                writer.execute("PRAGMA journal_mode=DELETE")
                if writer.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("invalid monitor snapshot")
        progress(0, 0, 0)
        pending.chmod(0o440)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def monitor_snapshot_loop(source, destination, stop, *, interval=30, report=None):
    """Run in the queue owner's process, independently of request transactions."""
    import logging

    if interval <= 0:
        raise ValueError("snapshot interval must be positive")
    logger = logging.getLogger(__name__)
    while not stop.is_set():
        try:
            refresh_monitor_snapshot(source, destination, stop=stop)
        except InterruptedError:
            if stop.is_set():
                break
            raise
        except Exception:
            # Exceptions may contain paths or database content.
            logger.error("monitor_snapshot_failed")
            if report:
                report(False)
        else:
            if report:
                report(True)
        stop.wait(interval)



def application():
    """Factory used by the production WSGI server."""
    import os

    role = os.environ["SUPPORT_AI_WEBHOOK_ROLE"]
    if role == "frontend":
        return queue_frontend("/ipc/queue.sock")
    return backend(role, monitor_max_age=120 if role == "monitor" else None)


def check_health():
    """Return readiness without creating events or revealing queue content."""
    import os
    from pathlib import Path

    role = os.environ["SUPPORT_AI_WEBHOOK_ROLE"]
    path = "/ipc/queue.sock" if role == "queue" else "/edge/backend.sock"
    connection = QueueConnection(path)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        expected = 200 if role == "frontend" else 404
        if response.status != expected:
            return False
        if role in {"queue", "monitor"}:
            age = time.time() - Path("/monitor/queue.sqlite").stat().st_mtime
            return 0 <= age <= 120
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
