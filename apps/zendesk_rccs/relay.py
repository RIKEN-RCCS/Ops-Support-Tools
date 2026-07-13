#!/usr/bin/env python3
"""R-CCS Zendesk OAuth API relay.

The relay keeps OAuth tokens out of support-ai workers, refreshes them as needed,
and forwards authenticated Zendesk API requests through a local Bearer-token
boundary. It never prints OAuth token values.
"""

from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from flask import Flask, Response, jsonify, request


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def read_secret(name: str, *, default: str = "") -> str:
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError):
            return default
    return os.environ.get(name, default).strip()


def parse_ts(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_bundle(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    token = out.get("access_token") or out.get("full_token") or out.get("token")
    if token:
        out["access_token"] = str(token)
    expires_at = parse_ts(out.get("expires_at"))
    if expires_at is None and out.get("expires_in") is not None:
        try:
            expires_at = int(time.time()) + int(out["expires_in"])
        except (TypeError, ValueError):
            expires_at = None
    if expires_at is not None:
        out["expires_at"] = iso_utc(expires_at)
    refresh_expires_at = parse_ts(out.get("refresh_token_expires_at"))
    if refresh_expires_at is None and out.get("refresh_token_expires_in") is not None:
        try:
            refresh_expires_at = int(time.time()) + int(out["refresh_token_expires_in"])
        except (TypeError, ValueError):
            refresh_expires_at = None
    if refresh_expires_at is not None:
        out["refresh_token_expires_at"] = iso_utc(refresh_expires_at)
    return out


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(normalize_bundle(data), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o640)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def parse_bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    zendesk_url: str
    client_identifier: str
    client_secret: str
    token_bundle_file: Path
    scopes: list[str]
    relay_token: str
    db_file: Path
    refresh_margin_seconds: int
    periodic_refresh_seconds: int
    default_public_comment: bool
    default_tags: list[str]
    ticket_subject_prefix: str
    timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        scopes_raw = os.environ.get("ZENDESK_RCCS_OAUTH_SCOPES", "read write")
        scopes = [item.strip() for item in scopes_raw.replace(",", " ").split() if item.strip()]
        data_dir = Path(os.environ.get("ZENDESK_RCCS_DATA_DIR", "/data"))
        return cls(
            zendesk_url=os.environ.get("ZENDESK_RCCS_URL", "https://r-ccs.zendesk.com").rstrip("/"),
            client_identifier=os.environ.get("ZENDESK_RCCS_OAUTH_CLIENT_IDENTIFIER", "").strip(),
            client_secret=read_secret("ZENDESK_RCCS_OAUTH_CLIENT_SECRET"),
            token_bundle_file=Path(os.environ.get(
                "ZENDESK_RCCS_OAUTH_TOKEN_BUNDLE_FILE",
                str(data_dir / "zendesk_oauth_token_bundle.json"),
            )),
            scopes=scopes or ["read", "write"],
            relay_token=read_secret("ZENDESK_RCCS_RELAY_TOKEN"),
            db_file=Path(os.environ.get("ZENDESK_RCCS_DB_FILE", str(data_dir / "events.sqlite"))),
            refresh_margin_seconds=int(os.environ.get("ZENDESK_RCCS_REFRESH_MARGIN_SECONDS", "600")),
            periodic_refresh_seconds=int(os.environ.get("ZENDESK_RCCS_PERIODIC_REFRESH_SECONDS", "1200")),
            default_public_comment=env_bool("ZENDESK_RCCS_DEFAULT_PUBLIC_COMMENT", False),
            default_tags=env_list("ZENDESK_RCCS_DEFAULT_TAGS", ["support_ai_relay"]),
            ticket_subject_prefix=os.environ.get("ZENDESK_RCCS_TICKET_SUBJECT_PREFIX", "[Support AI]"),
            timeout=float(os.environ.get("ZENDESK_RCCS_HTTP_TIMEOUT", "30")),
        )

    def validate(self) -> None:
        missing = []
        if not self.zendesk_url:
            missing.append("ZENDESK_RCCS_URL")
        if not self.client_identifier:
            missing.append("ZENDESK_RCCS_OAUTH_CLIENT_IDENTIFIER")
        if not self.client_secret:
            missing.append("ZENDESK_RCCS_OAUTH_CLIENT_SECRET or *_FILE")
        if not self.relay_token:
            missing.append("ZENDESK_RCCS_RELAY_TOKEN or *_FILE")
        if missing:
            raise RuntimeError("missing config: " + ", ".join(missing))


class OAuthClient:
    def __init__(self, config: Config):
        self.config = config
        self.lock_file = config.token_bundle_file.with_suffix(config.token_bundle_file.suffix + ".lock")

    def _load_bundle(self) -> dict[str, Any]:
        data = json.loads(self.config.token_bundle_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("OAuth token bundle is not a JSON object")
        return normalize_bundle(data)

    def _refresh_locked(self, bundle: dict[str, Any]) -> dict[str, Any]:
        refresh_token = str(bundle.get("refresh_token") or "")
        if not refresh_token:
            raise RuntimeError("OAuth token bundle has no refresh_token")
        response = requests.post(
            self.config.zendesk_url + "/oauth/tokens",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.config.client_identifier,
                "client_secret": self.config.client_secret,
                "scope": " ".join(self.config.scopes),
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=self.config.timeout,
        )
        if not response.ok:
            raise RuntimeError(f"Zendesk OAuth refresh -> {response.status_code}: {response.text[:500]}")
        refreshed = normalize_bundle(response.json())
        merged = dict(bundle)
        merged.update(refreshed)
        atomic_write_json(self.config.token_bundle_file, merged)
        return normalize_bundle(merged)

    def ensure_access_token(self, *, force: bool = False) -> str:
        self.config.token_bundle_file.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_file.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            bundle = self._load_bundle()
            expires_at = parse_ts(bundle.get("expires_at"))
            should_refresh = force or expires_at is None or (
                expires_at - int(time.time()) <= self.config.refresh_margin_seconds
            )
            if should_refresh:
                bundle = self._refresh_locked(bundle)
                log("OAuth token bundle refreshed")
            access_token = str(bundle.get("access_token") or "")
            if not access_token:
                raise RuntimeError("OAuth token bundle has no access_token")
            return access_token


class EventStore:
    def __init__(self, db_file: Path):
        self.db_file = db_file
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    zendesk_ticket_id INTEGER,
                    requester_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def reserve(self, event_id: str, source: str) -> bool:
        now = iso_utc(int(time.time()))
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO events(event_id, source, status, created_at, updated_at)
                    VALUES (?, ?, 'received', ?, ?)
                    """,
                    (event_id, source, now, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def finish(
        self,
        event_id: str,
        *,
        status: str,
        zendesk_ticket_id: int | None = None,
        requester_id: int | None = None,
        error: str = "",
    ) -> None:
        now = iso_utc(int(time.time()))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE events
                SET status = ?, zendesk_ticket_id = COALESCE(?, zendesk_ticket_id),
                    requester_id = COALESCE(?, requester_id), updated_at = ?, error = ?
                WHERE event_id = ?
                """,
                (status, zendesk_ticket_id, requester_id, now, error[:500], event_id),
            )

    def record_proxy(
        self,
        *,
        event_id: str,
        source: str,
        status: str,
        zendesk_ticket_id: int | None = None,
        requester_id: int | None = None,
        error: str = "",
    ) -> None:
        now = iso_utc(int(time.time()))
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO events(event_id, source, status, zendesk_ticket_id,
                                       requester_id, created_at, updated_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, source, status, zendesk_ticket_id, requester_id, now, now, error[:500]),
                )
        except sqlite3.IntegrityError:
            self.finish(
                event_id,
                status=status,
                zendesk_ticket_id=zendesk_ticket_id,
                requester_id=requester_id,
                error=error,
            )

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, source, status, zendesk_ticket_id, requester_id,
                       created_at, updated_at, error
                FROM events
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class ZendeskClient:
    def __init__(self, config: Config, oauth: OAuthClient):
        self.config = config
        self.oauth = oauth

    def request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.oauth.ensure_access_token()
        response = requests.request(
            method.upper(),
            self.config.zendesk_url + path,
            json=json_body,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
        )
        if response.status_code == 401:
            token = self.oauth.ensure_access_token(force=True)
            response = requests.request(
                method.upper(),
                self.config.zendesk_url + path,
                json=json_body,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=self.config.timeout,
            )
        if not response.ok:
            raise RuntimeError(f"Zendesk {method} {path} -> {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        return response.json()

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        content_type: str = "",
    ) -> requests.Response:
        token = self.oauth.ensure_access_token()
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": request.headers.get("Accept", "application/json"),
        }
        if content_type:
            headers["Content-Type"] = content_type
        response = requests.request(
            method.upper(),
            self.config.zendesk_url + path,
            params=params,
            data=body,
            headers=headers,
            timeout=self.config.timeout,
        )
        if response.status_code == 401:
            token = self.oauth.ensure_access_token(force=True)
            headers["Authorization"] = "Bearer " + token
            response = requests.request(
                method.upper(),
                self.config.zendesk_url + path,
                params=params,
                data=body,
                headers=headers,
                timeout=self.config.timeout,
            )
        return response

    def resolve_user(self, identity: dict[str, Any]) -> dict[str, Any]:
        user_id = identity.get("zendesk_user_id") or identity.get("user_id")
        if user_id:
            data = self.request("GET", f"/api/v2/users/{int(user_id)}.json")
            return data.get("user") or {}
        queries = []
        email = str(identity.get("email") or "").strip()
        external_id = str(identity.get("external_id") or identity.get("uid") or "").strip()
        if email:
            queries.append(email)
        if external_id:
            queries.append(f'external_id:"{external_id}"')
            queries.append(external_id)
        for query in queries:
            data = self.request("GET", f"/api/v2/users/search.json?query={requests.utils.quote(query)}")
            users = data.get("users") or []
            if len(users) == 1:
                return users[0]
        raise RuntimeError("Zendesk requester could not be resolved uniquely")

    def create_ticket(
        self,
        *,
        requester_id: int,
        subject: str,
        body: str,
        tags: list[str],
        public: bool,
    ) -> dict[str, Any]:
        payload = {
            "ticket": {
                "requester_id": requester_id,
                "subject": subject,
                "comment": {"body": body, "public": public},
                "tags": tags,
            }
        }
        return self.request("POST", "/api/v2/tickets.json", json_body=payload).get("ticket") or {}

    def add_ticket_comment(self, *, ticket_id: int, body: str, public: bool, tags: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {"ticket": {"comment": {"body": body, "public": public}}}
        if tags:
            ticket = self.request("GET", f"/api/v2/tickets/{ticket_id}.json").get("ticket") or {}
            current = {str(tag) for tag in ticket.get("tags") or []}
            current.update(tags)
            payload["ticket"]["tags"] = sorted(current)
        return self.request("PUT", f"/api/v2/tickets/{ticket_id}.json", json_body=payload).get("ticket") or {}


def build_app(config: Config) -> Flask:
    app = Flask(__name__)
    oauth = OAuthClient(config)
    zendesk = ZendeskClient(config, oauth)
    store = EventStore(config.db_file)

    def missing_config() -> list[str]:
        missing = []
        if not config.zendesk_url:
            missing.append("ZENDESK_RCCS_URL")
        if not config.client_identifier:
            missing.append("ZENDESK_RCCS_OAUTH_CLIENT_IDENTIFIER")
        if not config.client_secret:
            missing.append("ZENDESK_RCCS_OAUTH_CLIENT_SECRET or *_FILE")
        if not config.relay_token:
            missing.append("ZENDESK_RCCS_RELAY_TOKEN or *_FILE")
        if not config.token_bundle_file.exists():
            missing.append("ZENDESK_RCCS_OAUTH_TOKEN_BUNDLE_FILE")
        return missing

    def authorized() -> bool:
        header = request.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
        token = token or request.headers.get("X-R-CCS-Relay-Token", "").strip()
        return bool(token) and hmac.compare_digest(token, config.relay_token)

    def require_ready_and_authorized() -> Response | None:
        missing = missing_config()
        if missing:
            return jsonify({"ok": False, "error": "not configured", "missing_config": missing}), 503
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return None

    def proxy_event_id(prefix: str) -> str:
        supplied = (
            request.headers.get("Idempotency-Key")
            or request.headers.get("X-Request-Id")
            or request.headers.get("X-Event-Id")
            or ""
        ).strip()
        return supplied or f"{prefix}-{uuid.uuid4()}"

    def proxy_response(response: requests.Response) -> Response:
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        return Response(response.content, status=response.status_code, content_type=content_type)

    def proxy_zendesk(
        method: str,
        path: str,
        *,
        event_prefix: str,
        params: dict[str, str] | None = None,
        body: bytes | None = None,
        content_type: str = "",
        ticket_id: int | None = None,
    ) -> Response:
        guard = require_ready_and_authorized()
        if guard is not None:
            return guard
        event_id = proxy_event_id(event_prefix)
        try:
            response = zendesk.raw_request(
                method,
                path,
                params=params,
                body=body,
                content_type=content_type,
            )
            store.record_proxy(
                event_id=event_id,
                source=event_prefix,
                status="proxied" if response.ok else "failed",
                zendesk_ticket_id=ticket_id,
                error="" if response.ok else f"Zendesk {response.status_code}: {response.text[:300]}",
            )
            return proxy_response(response)
        except Exception as exc:  # noqa: BLE001
            store.record_proxy(event_id=event_id, source=event_prefix, status="failed", error=str(exc))
            log(f"proxy failed source={event_prefix}: {exc}")
            return jsonify({"ok": False, "error": str(exc)[:300]}), 502

    @app.get("/health")
    def health():
        missing = missing_config()
        return jsonify({
            "ok": not missing,
            "service": "zendesk_rccs",
            "zendesk_url": config.zendesk_url,
            "missing_config": missing,
        }), 200 if not missing else 503

    @app.get("/api/v2/search.json")
    def proxy_search():
        query = request.args.get("query", "")
        return proxy_zendesk(
            "GET",
            "/api/v2/search.json",
            event_prefix="search",
            params={"query": query},
        )

    @app.get("/api/v2/organizations/search.json")
    def proxy_organization_search():
        name = request.args.get("name", "")
        return proxy_zendesk(
            "GET",
            "/api/v2/organizations/search.json",
            event_prefix="organization_search",
            params={"name": name},
        )

    @app.post("/api/v2/tickets.json")
    def proxy_ticket_create():
        return proxy_zendesk(
            "POST",
            "/api/v2/tickets.json",
            event_prefix="ticket_create",
            body=request.get_data(),
            content_type=request.headers.get("Content-Type", "application/json"),
        )

    @app.route("/api/v2/tickets/<int:ticket_id>.json", methods=["PUT", "PATCH", "POST"])
    def proxy_ticket_update(ticket_id: int):
        method = "PUT" if request.method == "POST" else request.method
        return proxy_zendesk(
            method,
            f"/api/v2/tickets/{ticket_id}.json",
            event_prefix="ticket_update",
            body=request.get_data(),
            content_type=request.headers.get("Content-Type", "application/json"),
            ticket_id=ticket_id,
        )

    @app.post("/api/v2/uploads.json")
    def proxy_upload():
        filename = request.args.get("filename", "")
        params = {"filename": filename} if filename else {}
        return proxy_zendesk(
            "POST",
            "/api/v2/uploads.json",
            event_prefix="upload",
            params=params,
            body=request.get_data(),
            content_type=request.headers.get("Content-Type", "application/binary"),
        )

    @app.route("/api/v2/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def proxy_generic_api(subpath: str):
        method = request.method
        path = "/api/v2/" + subpath
        return proxy_zendesk(
            method,
            path,
            event_prefix="api_proxy",
            params=request.args.to_dict(flat=True) if request.args else None,
            body=request.get_data() if method in {"POST", "PUT", "PATCH", "DELETE"} else None,
            content_type=request.headers.get("Content-Type", "application/json"),
        )

    @app.get("/events")
    def recent_events():
        if not config.relay_token:
            return jsonify({"ok": False, "error": "not configured", "missing_config": missing_config()}), 503
        if not authorized():
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            limit = 50
        return jsonify({"ok": True, "events": store.recent(limit=limit)})

    return app


def periodic_refresh(config: Config) -> None:
    if config.periodic_refresh_seconds <= 0:
        log("periodic OAuth refresh disabled")
        return
    oauth = OAuthClient(config)
    while True:
        time.sleep(config.periodic_refresh_seconds)
        try:
            oauth.ensure_access_token()
        except Exception as exc:  # noqa: BLE001
            log(f"periodic OAuth refresh failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="R-CCS Zendesk OAuth API relay")
    parser.add_argument("--host", default=os.environ.get("ZENDESK_RCCS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ZENDESK_RCCS_PORT", "8080")))
    args = parser.parse_args()
    config = Config.from_env()
    app = build_app(config)
    thread = threading.Thread(target=periodic_refresh, args=(config,), daemon=True)
    thread.start()
    log(f"starting zendesk_rccs relay for {config.zendesk_url}")
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
