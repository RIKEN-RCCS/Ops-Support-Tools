#!/usr/bin/env python3
"""Standalone Zendesk OAuth helper without legacy API-token calls.

This script intentionally does not run as a web service and does not print
token values. It handles only OAuth authorization-code exchange, refresh,
inspection, and API checks with an existing OAuth token bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_secret(name: str, *, default: str = "") -> str:
    file_name = os.environ.get(f"{name}_FILE", "").strip()
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default).strip()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
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


def redacted_metadata(bundle: dict[str, Any]) -> dict[str, Any]:
    access = str(bundle.get("access_token") or "")
    refresh = str(bundle.get("refresh_token") or "")
    return {
        "id": bundle.get("id"),
        "user_id": bundle.get("user_id"),
        "client_id": bundle.get("client_id"),
        "scope": bundle.get("scope"),
        "scopes": bundle.get("scopes"),
        "created_at": bundle.get("created_at"),
        "expires_at": bundle.get("expires_at"),
        "refresh_token_expires_at": bundle.get("refresh_token_expires_at"),
        "has_access_token": bool(access),
        "access_token_len": len(access),
        "has_refresh_token": bool(refresh),
        "refresh_token_len": len(refresh),
        "refresh_token_looks_redacted": refresh.startswith("..."),
    }


@dataclass
class Config:
    zendesk_url: str
    client_identifier: str
    client_secret: str
    redirect_uri: str
    token_bundle_file: Path
    scopes: list[str]
    timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        scopes_raw = os.environ.get("ZENDESK_OAUTH_SCOPES", "read write")
        scopes = [item.strip() for item in scopes_raw.replace(",", " ").split() if item.strip()]
        return cls(
            zendesk_url=os.environ.get("ZENDESK_URL", "").rstrip("/"),
            client_identifier=os.environ.get("ZENDESK_OAUTH_CLIENT_IDENTIFIER", "ops-support-tools_zendesk_api"),
            client_secret=read_secret("ZENDESK_OAUTH_CLIENT_SECRET"),
            redirect_uri=os.environ.get("ZENDESK_OAUTH_REDIRECT_URI", "http://localhost"),
            token_bundle_file=Path(os.environ.get(
                "ZENDESK_OAUTH_TOKEN_BUNDLE_FILE",
                "apps/zendesk-oauth-tools/secrets/zendesk_oauth_token_bundle.json",
            )),
            scopes=scopes or ["read", "write"],
            timeout=float(os.environ.get("ZENDESK_HTTP_TIMEOUT", "30")),
        )

    def validate_client(self) -> None:
        missing = []
        if not self.zendesk_url:
            missing.append("ZENDESK_URL")
        if not self.client_identifier:
            missing.append("ZENDESK_OAUTH_CLIENT_IDENTIFIER")
        if not self.client_secret:
            missing.append("ZENDESK_OAUTH_CLIENT_SECRET or ZENDESK_OAUTH_CLIENT_SECRET_FILE")
        if not self.redirect_uri:
            missing.append("ZENDESK_OAUTH_REDIRECT_URI")
        if missing:
            raise SystemExit("missing config: " + ", ".join(missing))


class ZendeskOAuthTool:
    def __init__(self, config: Config):
        self.config = config

    def authorize_url(self, *, state: str | None = None) -> str:
        self.config.validate_client()
        params = {
            "response_type": "code",
            "client_id": self.config.client_identifier,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state or secrets.token_urlsafe(24),
        }
        return self.config.zendesk_url + "/oauth/authorizations/new?" + urlencode(params)

    def exchange_code(self, code: str) -> dict[str, Any]:
        self.config.validate_client()
        response = requests.post(
            self.config.zendesk_url + "/oauth/tokens",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.config.client_identifier,
                "client_secret": self.config.client_secret,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(self.config.scopes),
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=self.config.timeout,
        )
        if not response.ok:
            raise RuntimeError(f"Zendesk OAuth code exchange -> {response.status_code}: {response.text[:500]}")
        return normalize_bundle(response.json())

    def request_bearer(self, method: str, path: str, *, access_token: str) -> dict[str, Any]:
        response = requests.request(
            method.upper(),
            self.config.zendesk_url + path,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + access_token,
            },
            timeout=self.config.timeout,
        )
        if not response.ok:
            raise RuntimeError(f"Zendesk {method} {path} -> {response.status_code}: {response.text[:500]}")
        if not response.content:
            return {}
        return response.json()

    def load_bundle(self) -> dict[str, Any]:
        data = json.loads(self.config.token_bundle_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("token bundle is not a JSON object")
        return normalize_bundle(data)

    def save_bundle(self, bundle: dict[str, Any]) -> None:
        atomic_write_text(
            self.config.token_bundle_file,
            json.dumps(normalize_bundle(bundle), ensure_ascii=False, indent=2),
        )

    def check(self, bundle: dict[str, Any]) -> dict[str, Any]:
        access_token = str(bundle.get("access_token") or "")
        if not access_token:
            raise RuntimeError("token bundle has no access_token/full_token/token")
        return self.request_bearer("GET", "/api/v2/users/me.json", access_token=access_token).get("user", {})

    def refresh(self, bundle: dict[str, Any]) -> dict[str, Any]:
        self.config.validate_client()
        refresh_token = str(bundle.get("refresh_token") or "")
        if not refresh_token:
            raise RuntimeError("token bundle has no refresh_token")
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
        return normalize_bundle(merged)


def print_json(data: dict[str, Any] | list[Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone Zendesk OAuth helper without legacy API-token calls")
    parser.add_argument("--env-file", type=Path, default=Path("apps/zendesk-oauth-tools/.env"))
    sub = parser.add_subparsers(dest="command", required=True)

    authorize = sub.add_parser("authorize-url", help="Print authorization URL for browser login")
    authorize.add_argument("--state", default="")

    exchange = sub.add_parser("exchange-code", help="Exchange authorization code and save token bundle")
    exchange.add_argument("--code", required=True)

    sub.add_parser("inspect", help="Show saved token bundle metadata without token values")
    sub.add_parser("check", help="Check Zendesk API with saved OAuth access token")
    sub.add_parser("refresh", help="Refresh saved OAuth token bundle and save it")

    args = parser.parse_args()
    load_env_file(args.env_file)
    tool = ZendeskOAuthTool(Config.from_env())

    if args.command == "authorize-url":
        print_json({
            "authorize_url": tool.authorize_url(state=args.state or None),
            "client_identifier": tool.config.client_identifier,
            "redirect_uri": tool.config.redirect_uri,
            "scopes": tool.config.scopes,
        })
        return 0

    if args.command == "exchange-code":
        bundle = tool.exchange_code(args.code)
        tool.save_bundle(bundle)
        print_json({
            "ok": True,
            "saved_to": str(tool.config.token_bundle_file),
            "metadata": redacted_metadata(bundle),
        })
        return 0

    if args.command == "inspect":
        print_json(redacted_metadata(tool.load_bundle()))
        return 0

    if args.command == "check":
        user = tool.check(tool.load_bundle())
        print_json({
            "ok": True,
            "user": {
                "id": user.get("id"),
                "role": user.get("role"),
                "role_type": user.get("role_type"),
            },
        })
        return 0

    if args.command == "refresh":
        refreshed = tool.refresh(tool.load_bundle())
        tool.save_bundle(refreshed)
        print_json({
            "ok": True,
            "saved_to": str(tool.config.token_bundle_file),
            "metadata": redacted_metadata(refreshed),
        })
        return 0

    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
