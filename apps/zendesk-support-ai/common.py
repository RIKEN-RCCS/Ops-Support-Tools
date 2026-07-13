"""共通基盤: 設定・スプール管理・Zendesk クライアント。

安全設計(spec §6)に従い、このモジュールの Zendesk 書き込み操作は
通常パイプラインでは `post_internal_note`(内部メモ追記 + タグ付与)に限定する。
例外として、明示 webhook から最新の内部メモをそのまま公開返信へ転送する
`post_public_reply` を提供する。クローズ / 標準 assignee 変更は行わない。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from secret_config import env_secret
import spool_crypto

# --------------------------------------------------------------------------
# 設定(すべて環境変数経由。秘密情報はコードに直書きしない — spec §6-7)
# --------------------------------------------------------------------------

# スプールの既定値はこのリポジトリ配下(spool)。
_DEFAULT_SPOOL = Path(__file__).resolve().parent / "spool"
SPOOL_DIR = Path(os.environ.get("SUPPORT_AI_QUEUE_DIR", str(_DEFAULT_SPOOL)))
QUEUE_DB = Path(os.environ.get("SUPPORT_AI_QUEUE_DB", str(SPOOL_DIR / "queue.sqlite")))

ZENDESK_URL = os.environ.get("ZENDESK_URL", "").rstrip("/")
ZENDESK_EMAIL = os.environ.get("ZENDESK_EMAIL", "")
# API トークン(値はログに出さない)。本番では ZENDESK_KEY_FILE を使う。
_ZENDESK_KEY = env_secret("ZENDESK_KEY")
ZENDESK_RELAY_URL = (
    os.environ.get("ZENDESK_RELAY_URL")
    or os.environ.get("ZENDESK_RCCS_RELAY_URL")
    or ""
).rstrip("/")
_ZENDESK_RELAY_TOKEN = env_secret("ZENDESK_RELAY_TOKEN") or env_secret("ZENDESK_RCCS_RELAY_TOKEN")

SPOOL_SUBDIRS = (
    "incoming",
    "incoming_followup",
    "pending",
    "pending_followup",
    "tmp",
    "done",
    "failed",
    "state",
)

# 担当割り当て名簿(SPEC_ASSIGNMENT.md §5)。秘密情報ではないが運用で頻繁に変わるため
# 設定ファイルで持つ(コード直書きしない)。既定は agents.json。
AGENTS_FILE = Path(os.environ.get("SUPPORT_AI_AGENTS_FILE", str(Path(__file__).resolve().parent / "agents.json")))

HTTP_TIMEOUT = float(os.environ.get("SUPPORT_AI_HTTP_TIMEOUT", "30"))
LIGHT_AGENT_ROLE_TYPE = int(os.environ.get("SUPPORT_AI_LIGHT_AGENT_ROLE_TYPE", "1"))
INCLUDE_SUSPENDED_AGENTS = os.environ.get("SUPPORT_AI_INCLUDE_SUSPENDED_AGENTS", "").lower() in ("1", "true", "yes")
AGENTS_TEMPLATE_FILE = Path(os.environ.get(
    "SUPPORT_AI_AGENTS_TEMPLATE_FILE",
    str(Path(__file__).resolve().parent / "agents.example.json"),
))
ASSIGNEE_FIELD_ID_ENV = os.environ.get("SUPPORT_AI_ASSIGNEE_FIELD_ID", "").strip()
KNOWLEDGE_API_URL = os.environ.get("SUPPORT_AI_KNOWLEDGE_API_URL", "").rstrip("/")


def log(*args: Any) -> None:
    """stderr へのタイムスタンプ付きログ。秘密情報は渡さないこと。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]", *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# SQLite キュー / レガシースプール管理(spec §3)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueItem:
    queue: str
    name: str

    def unlink(self) -> None:
        delete_queue_item(self)


def _queue_conn() -> sqlite3.Connection:
    QUEUE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(QUEUE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_queue_db() -> None:
    with _queue_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
              queue TEXT NOT NULL,
              name TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (queue, name)
            )
            """
        )


def _encode_queue_payload(data: Dict[str, Any]) -> str:
    key = spool_crypto.load_key()
    payload = spool_crypto.encrypt_json(data, key=key) if key else data
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _decode_queue_payload(raw: str) -> Dict[str, Any]:
    data = json.loads(raw)
    if spool_crypto.is_encrypted_json(data):
        key = spool_crypto.load_key()
        if not key:
            raise RuntimeError("queue payload is encrypted but SUPPORT_AI_QUEUE_KEY is not configured")
        return spool_crypto.decrypt_json(data, key=key)
    return data


def ensure_spool_dirs(base: Optional[Path] = None) -> Path:
    """レガシースプールの全サブディレクトリと SQLite キューを作成し、ベースパスを返す。"""
    base = Path(base) if base is not None else SPOOL_DIR
    for sub in SPOOL_SUBDIRS:
        (base / sub).mkdir(parents=True, exist_ok=True)
    _init_queue_db()
    return base


def spool_path(subdir: str, base: Optional[Path] = None) -> Path:
    base = Path(base) if base is not None else SPOOL_DIR
    if subdir not in SPOOL_SUBDIRS:
        raise ValueError(f"unknown spool subdir: {subdir}")
    return base / subdir


def _queue_for_target(target: Path, *, base: Path) -> Optional[str]:
    target_parent = target.parent.resolve()
    base_resolved = base.resolve()
    for subdir in SPOOL_SUBDIRS:
        if target_parent == (base_resolved / subdir):
            return subdir
    return None


def queue_exists(queue: str, name: str) -> bool:
    _init_queue_db()
    with _queue_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM queue_items WHERE queue = ? AND name = ?",
            (queue, name),
        ).fetchone()
    return row is not None


def write_queue_item(queue: str, name: str, data: Dict[str, Any]) -> QueueItem:
    if queue not in SPOOL_SUBDIRS:
        raise ValueError(f"unknown queue: {queue}")
    _init_queue_db()
    now = int(time.time())
    with _queue_conn() as conn:
        conn.execute(
            """
            INSERT INTO queue_items (queue, name, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (queue, name, _encode_queue_payload(data), now, now),
        )
    return QueueItem(queue=queue, name=name)


def update_queue_item(item: QueueItem, data: Dict[str, Any]) -> QueueItem:
    _init_queue_db()
    now = int(time.time())
    with _queue_conn() as conn:
        cur = conn.execute(
            """
            UPDATE queue_items
            SET payload_json = ?, updated_at = ?
            WHERE queue = ? AND name = ?
            """,
            (_encode_queue_payload(data), now, item.queue, item.name),
        )
    if cur.rowcount == 0:
        raise FileNotFoundError(f"queue item not found: {item.queue}/{item.name}")
    return item


def read_queue_item(item: QueueItem) -> Dict[str, Any]:
    _init_queue_db()
    with _queue_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM queue_items WHERE queue = ? AND name = ?",
            (item.queue, item.name),
        ).fetchone()
    if not row:
        raise FileNotFoundError(f"queue item not found: {item.queue}/{item.name}")
    return _decode_queue_payload(row["payload_json"])


def list_queue(queue: str, pattern: str = "*.json") -> List[QueueItem | Path]:
    if queue not in SPOOL_SUBDIRS:
        raise ValueError(f"unknown queue: {queue}")
    _init_queue_db()
    with _queue_conn() as conn:
        rows = conn.execute(
            "SELECT name FROM queue_items WHERE queue = ? ORDER BY created_at ASC, name ASC",
            (queue,),
        ).fetchall()
    items: List[QueueItem | Path] = [
        QueueItem(queue=queue, name=row["name"]) for row in rows if fnmatch(row["name"], pattern)
    ]
    queued_names = {item.name for item in items if isinstance(item, QueueItem)}
    legacy_dir = spool_path(queue)
    if legacy_dir.exists():
        items.extend(path for path in sorted(legacy_dir.glob(pattern)) if path.name not in queued_names)
    return items


def delete_queue_item(item: QueueItem) -> None:
    _init_queue_db()
    with _queue_conn() as conn:
        conn.execute(
            "DELETE FROM queue_items WHERE queue = ? AND name = ?",
            (item.queue, item.name),
        )


def move_queue_item(item: QueueItem, queue: str) -> QueueItem:
    if queue not in SPOOL_SUBDIRS:
        raise ValueError(f"unknown queue: {queue}")
    _init_queue_db()
    now = int(time.time())
    with _queue_conn() as conn:
        conn.execute(
            """
            UPDATE queue_items
            SET queue = ?, updated_at = ?
            WHERE queue = ? AND name = ?
            """,
            (queue, now, item.queue, item.name),
        )
    return QueueItem(queue=queue, name=item.name)


def atomic_write_json(target: Path, data: Dict[str, Any], *, base: Optional[Path] = None) -> Path | QueueItem:
    """tmp/ に書いてから os.rename() で目的ディレクトリへアトミックに移す(spec §3 鉄則)。

    目的ディレクトリへ直接書かない。書きかけファイルを他プロセスが拾わないことを保証する。
    """
    base = Path(base) if base is not None else SPOOL_DIR
    target = Path(target)
    queue = _queue_for_target(target, base=base)
    if queue and queue not in {"tmp", "state"}:
        return write_queue_item(queue, target.name, data)

    tmp_dir = base / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=target.stem + "_", suffix=".tmp", dir=str(tmp_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            key = spool_crypto.load_key()
            payload = spool_crypto.encrypt_json(data, key=key) if key else data
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_name, target)  # 同一 FS 上のアトミック rename
    except Exception:
        # 失敗時は tmp を残さない
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def move_to(src: Path | QueueItem, subdir: str, *, base: Optional[Path] = None) -> Path | QueueItem:
    """既存ファイルをスプール内の別サブディレクトリへアトミックに移す。"""
    if isinstance(src, QueueItem):
        return move_queue_item(src, subdir)
    base = Path(base) if base is not None else SPOOL_DIR
    src = Path(src)
    dest_dir = spool_path(subdir, base=base)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    os.rename(src, dest)
    return dest


def read_json(path: Path | QueueItem) -> Dict[str, Any]:
    if isinstance(path, QueueItem):
        return read_queue_item(path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if spool_crypto.is_encrypted_json(data):
        key = spool_crypto.load_key()
        if not key:
            raise RuntimeError(f"{path} is encrypted but SUPPORT_AI_QUEUE_KEY is not configured")
        return spool_crypto.decrypt_json(data, key=key)
    return data


def atomic_write_json_same_dir(target: Path, data: Dict[str, Any]) -> Path:
    """任意の JSON ファイルを同じディレクトリ内の tmp 経由で atomic に更新する。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.stem + "_", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_name, target)
        os.chmod(target, 0o664)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


# --------------------------------------------------------------------------
# Zendesk クライアント
# --------------------------------------------------------------------------

def _zd_auth():
    """API トークン認証: (email/token, api_token)。"""
    if not ZENDESK_URL:
        raise RuntimeError("ZENDESK_URL が未設定です")
    if not ZENDESK_EMAIL or not _ZENDESK_KEY:
        raise RuntimeError("ZENDESK_EMAIL / ZENDESK_KEY が未設定です")
    return (f"{ZENDESK_EMAIL}/token", _ZENDESK_KEY)


def _use_zendesk_relay() -> bool:
    return bool(ZENDESK_RELAY_URL)


def _zd_path_from_url_or_path(value: str) -> str:
    """Zendesk/relay の full URL または path を '/api/v2/...' 形式へ寄せる。"""
    text = str(value or "").strip()
    if not text:
        return text
    if ZENDESK_URL and text.startswith(ZENDESK_URL):
        text = text[len(ZENDESK_URL):]
    elif ZENDESK_RELAY_URL and text.startswith(ZENDESK_RELAY_URL):
        text = text[len(ZENDESK_RELAY_URL):]
    elif text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        text = parsed.path
        if parsed.query:
            text += "?" + parsed.query
    return text if text.startswith("/") else "/" + text


def _zd_url(path: str) -> str:
    normalized = _zd_path_from_url_or_path(path)
    if _use_zendesk_relay():
        return ZENDESK_RELAY_URL + normalized
    if not ZENDESK_URL:
        raise RuntimeError("ZENDESK_URL が未設定です")
    return ZENDESK_URL + normalized


def zd_request(method: str, path: str, *, params: Optional[dict] = None,
               json_body: Optional[dict] = None) -> Dict[str, Any]:
    """Zendesk REST API への薄いラッパ。path は '/api/v2/...' で渡す。"""
    url = _zd_url(path)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth = None
    if _use_zendesk_relay():
        if not _ZENDESK_RELAY_TOKEN:
            raise RuntimeError("ZENDESK_RELAY_TOKEN / ZENDESK_RELAY_TOKEN_FILE が未設定です")
        headers["Authorization"] = "Bearer " + _ZENDESK_RELAY_TOKEN
    else:
        auth = _zd_auth()
    resp = requests.request(
        method.upper(), url,
        auth=auth,
        params=params,
        json=json_body,
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        # ボディに秘密情報は含まれない想定だが、念のため短縮
        raise RuntimeError(f"Zendesk {method} {path} -> {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def search_tickets(query: str) -> List[Dict[str, Any]]:
    """Search API。読み取り専用。"""
    out: List[Dict[str, Any]] = []
    params = {"query": query}
    path = "/api/v2/search.json"
    while True:
        data = zd_request("GET", path, params=params)
        out.extend(data.get("results", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        # next_page はフル URL のため、relay 経由でも再利用できる path へ寄せる。
        path = _zd_path_from_url_or_path(next_page)
        params = None
    return out


def fetch_ticket_comments(ticket_id: int) -> List[Dict[str, Any]]:
    """チケットのコメントを取得(読み取り専用)。

    audit ではなく Comments API を使う(audit の metadata には IP 等が含まれるため — spec §5)。
    """
    out: List[Dict[str, Any]] = []
    path = f"/api/v2/tickets/{ticket_id}/comments.json"
    params: Optional[dict] = {}
    while True:
        data = zd_request("GET", path, params=params)
        out.extend(data.get("comments", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        path = _zd_path_from_url_or_path(next_page)
        params = None
    return out


def fetch_ticket(ticket_id: int) -> Dict[str, Any]:
    """チケット本体(subject 等)を取得。読み取り専用。"""
    return zd_request("GET", f"/api/v2/tickets/{ticket_id}.json").get("ticket", {})


def zendesk_healthcheck() -> Dict[str, Any]:
    """Zendesk API の疎通確認。OAuth relay 設定時は relay 経由で確認する。"""
    return zd_request("GET", "/api/v2/users/me.json").get("user", {})


def list_users(*, role: Optional[str] = None) -> List[Dict[str, Any]]:
    """Zendesk users をページングして取得する。"""
    out: List[Dict[str, Any]] = []
    params: Optional[dict] = {"role[]": role} if role else {}
    path = "/api/v2/users.json"
    while True:
        data = zd_request("GET", path, params=params)
        out.extend(data.get("users", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        path = _zd_path_from_url_or_path(next_page)
        params = None
    return out


# --------------------------------------------------------------------------
# 担当割り当て: 名簿・輪番状態・担当者フィールド書込(SPEC_ASSIGNMENT.md §5, §9)
# --------------------------------------------------------------------------

_agents_cache: Optional[Dict[str, Any]] = None


def ensure_agents_file() -> Path:
    """agents.json が無ければテンプレートから初期生成する。"""
    if AGENTS_FILE.exists():
        return AGENTS_FILE
    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if AGENTS_TEMPLATE_FILE.exists():
        loaded = read_json(AGENTS_TEMPLATE_FILE)
    else:
        loaded = {
            "_note": "Auto-generated initial agents config.",
            "assignee_field_id": None,
            "escalation_map": {
                "scheduler": None,
                "storage": None,
                "network": None,
                "software": None,
                "ondemand": None,
                "account": None,
                "other": None,
            },
            "light_agents": [],
        }
    atomic_write_json_same_dir(AGENTS_FILE, loaded)
    return AGENTS_FILE


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _agent_lookup(light_agents: List[Dict[str, Any]]) -> Dict[str, int]:
    lookup: Dict[str, int] = {}
    for agent in light_agents:
        aid = agent.get("id")
        if aid is None:
            continue
        for key in ("name", "email", "alias"):
            normalized = _norm_name(agent.get(key))
            if normalized:
                lookup[normalized] = int(aid)
    return lookup


def resolve_escalation_map(raw_map: Dict[str, Any], light_agents: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """人名/email/id 混在の escalation_map を Zendesk user id に解決する。"""
    lookup = _agent_lookup(light_agents)
    resolved: Dict[str, Any] = {}
    errors: List[str] = []

    def resolve_one(category: str, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            normalized = _norm_name(value)
            if normalized.isdigit():
                return int(normalized)
            if normalized in lookup:
                return lookup[normalized]
            errors.append(f"{category}: {value!r} を light_agents から解決できません")
            return None
        errors.append(f"{category}: unsupported escalation value {value!r}")
        return None

    for category, spec in (raw_map or {}).items():
        if isinstance(spec, list):
            ids = [aid for aid in (resolve_one(category, item) for item in spec) if aid is not None]
            resolved[category] = ids or None
        else:
            resolved[category] = resolve_one(category, spec)
    return resolved, errors


def load_agents_config(*, refresh: bool = False) -> Dict[str, Any]:
    """名簿(agents.json)を読む。

    返す dict: {assignee_field_id, light_agents:[{id,name,email}], escalation_map:{category:id|null}}。
    ファイルが無い/壊れている場合は安全な空設定を返す(機能は無効化され、メモ投稿は従来どおり)。
    """
    global _agents_cache
    if _agents_cache is not None and not refresh:
        return _agents_cache
    cfg: Dict[str, Any] = {"assignee_field_id": None, "light_agents": [], "escalation_map": {}}
    try:
        if AGENTS_FILE.exists():
            loaded = read_json(AGENTS_FILE)
            cfg["assignee_field_id"] = ASSIGNEE_FIELD_ID_ENV or loaded.get("assignee_field_id")
            cfg["light_agents"] = loaded.get("light_agents") or []
            raw_map = loaded.get("escalation_map") or {}
            cfg["escalation_map_raw"] = raw_map
            cfg["escalation_map"], errors = resolve_escalation_map(raw_map, cfg["light_agents"])
            if errors:
                cfg["escalation_errors"] = errors
                for err in errors:
                    log(f"escalation_map 解決警告: {err}")
    except Exception as e:  # noqa: BLE001
        log(f"agents.json 読込失敗(担当割り当てを無効化): {e}")
    _agents_cache = cfg
    return cfg


def fetch_light_agents() -> List[Dict[str, Any]]:
    """Zendesk から現在のライトエージェント一覧を取得する。"""
    users = list_users(role="agent")
    agents: List[Dict[str, Any]] = []
    for user in users:
        if user.get("role_type") != LIGHT_AGENT_ROLE_TYPE:
            continue
        if not INCLUDE_SUSPENDED_AGENTS and user.get("suspended"):
            continue
        agents.append({
            "id": int(user["id"]),
            "name": user.get("name") or str(user["id"]),
            "email": user.get("email"),
            "alias": user.get("alias"),
            "role_type": user.get("role_type"),
        })
    agents.sort(key=lambda a: (_norm_name(a.get("name")), int(a["id"])))
    return agents


def sync_agents_config(*, dry_run: bool = False) -> Dict[str, Any]:
    """agents.json の light_agents を Zendesk の現在値で更新する。

    escalation_map は人間が管理するため保持する。人名指定が現在の light_agents で
    解決できるかも検証し、解決後の設定を返す。
    """
    loaded: Dict[str, Any] = {}
    if AGENTS_FILE.exists():
        loaded = read_json(AGENTS_FILE)
    loaded.setdefault("assignee_field_id", None)
    if ASSIGNEE_FIELD_ID_ENV:
        loaded["assignee_field_id"] = ASSIGNEE_FIELD_ID_ENV
    loaded.setdefault("escalation_map", {})
    loaded["light_agents"] = fetch_light_agents()
    resolved, errors = resolve_escalation_map(loaded.get("escalation_map") or {}, loaded["light_agents"])
    loaded["_sync"] = {
        "source": "zendesk",
        "synced_at": int(time.time()),
        "light_agent_count": len(loaded["light_agents"]),
        "unresolved_escalations": errors,
    }
    if not dry_run:
        atomic_write_json_same_dir(AGENTS_FILE, loaded)
        load_agents_config(refresh=True)
    return {
        "assignee_field_id": loaded.get("assignee_field_id"),
        "light_agents": loaded["light_agents"],
        "escalation_map": resolved,
        "escalation_map_raw": loaded.get("escalation_map") or {},
        "escalation_errors": errors,
    }


def light_agent_ids() -> set:
    """書き込み可能 ID の allowlist(LIGHT_AGENTS の id 集合)。"""
    return {a["id"] for a in load_agents_config().get("light_agents", []) if "id" in a}


def agent_name(agent_id: int) -> str:
    for a in load_agents_config().get("light_agents", []):
        if a.get("id") == agent_id:
            return a.get("name", str(agent_id))
    return str(agent_id)


def _roundrobin_path(base: Optional[Path] = None) -> Path:
    base = Path(base) if base is not None else SPOOL_DIR
    return base / "state" / "roundrobin.json"


def read_roundrobin(*, base: Optional[Path] = None) -> int:
    """輪番カーソルを読む。未初期化なら 0。"""
    p = _roundrobin_path(base)
    try:
        return int(read_json(p).get("cursor", 0))
    except Exception:  # noqa: BLE001
        return 0


def write_roundrobin(cursor: int, *, base: Optional[Path] = None) -> None:
    """輪番カーソルを atomic に書く(tmp→rename)。"""
    base = Path(base) if base is not None else SPOOL_DIR
    atomic_write_json(_roundrobin_path(base), {"cursor": int(cursor)}, base=base)


def set_assignee_field(ticket_id: int, agent_id: int) -> Dict[str, Any]:
    """カスタムフィールド「担当者」にライトエージェント ID をセットする。

    SPEC_ASSIGNMENT.md §6-2 の例外的な 2 つ目の書き込み操作。
    assignee(標準の担当者)変更・クローズ・公開返信は依然として行わない。
    書き込む値は呼び出し側で allowlist 検証済みのライトエージェント ID であることを前提とする。
    ASSIGNEE_FIELD_ID(=agents.json の assignee_field_id)未設定なら書き込まない。
    """
    field_id = load_agents_config().get("assignee_field_id")
    if not field_id:
        raise RuntimeError("assignee_field_id が未設定です(Zendesk にカスタムフィールド『担当者』を作成して設定する)")
    return zd_request(
        "PUT", f"/api/v2/tickets/{ticket_id}.json",
        json_body={"ticket": {"custom_fields": [{"id": field_id, "value": agent_id}]}},
    )


def post_internal_note(ticket_id: int, body: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """当該チケットへ内部メモ(public:false)を追記し、タグを付与する。

    spec §6: クローズ/アサイン/公開返信は行わない。
    """
    comment: Dict[str, Any] = {"body": body, "public": False}
    ticket_update: Dict[str, Any] = {"comment": comment}
    if tags:
        current = fetch_ticket(ticket_id).get("tags") or []
        ticket_update["tags"] = sorted(set(str(tag) for tag in current + list(tags) if tag))
    result = zd_request(
        "PUT", f"/api/v2/tickets/{ticket_id}.json",
        json_body={"ticket": ticket_update},
    )
    return result


def post_public_reply(
    ticket_id: int,
    body: str,
    tags: Optional[List[str]] = None,
    remove_tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """当該チケットへ公開返信(public:true)を追記し、タグを付与する。

    AI は介在させず、呼び出し側が渡した本文をそのまま Zendesk へ送る。
    既存タグを消さないよう、tags/remove_tags は現在のタグへ差分適用する。
    """
    comment: Dict[str, Any] = {"body": body, "public": True}
    ticket_update: Dict[str, Any] = {"comment": comment}
    if tags or remove_tags:
        current = fetch_ticket(ticket_id).get("tags") or []
        remove = {str(tag) for tag in (remove_tags or []) if tag}
        merged = {str(tag) for tag in current if tag and str(tag) not in remove}
        merged.update(str(tag) for tag in (tags or []) if tag)
        ticket_update["tags"] = sorted(merged)
    return zd_request(
        "PUT", f"/api/v2/tickets/{ticket_id}.json",
        json_body={"ticket": ticket_update},
    )


# --------------------------------------------------------------------------
# Knowledge API client
# --------------------------------------------------------------------------

def knowledge_enabled() -> bool:
    return bool(KNOWLEDGE_API_URL)


def knowledge_create_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Knowledge API に run request を作成する。

    Knowledge API URL は内部ネットワーク向けの非秘密設定。失敗時の扱いは呼び出し側で決める。
    """
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.post(
        KNOWLEDGE_API_URL + "/api/runs",
        json=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API POST /api/runs -> {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def knowledge_list_runs(
    *,
    ticket_id: Optional[int] = None,
    status: str = "",
    parent_run_id: str = "",
    task_type: str = "",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Knowledge API の run 一覧を取得する。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    params: Dict[str, Any] = {"limit": int(limit)}
    if ticket_id is not None:
        params["ticket_id"] = int(ticket_id)
    if status:
        params["status"] = status
    if parent_run_id:
        params["parent_run_id"] = parent_run_id
    if task_type:
        params["task_type"] = task_type
    resp = requests.get(
        KNOWLEDGE_API_URL + "/api/runs",
        params=params,
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API GET /api/runs -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    runs = data.get("runs") if isinstance(data, dict) else []
    return runs if isinstance(runs, list) else []


def knowledge_worker_claim_run(
    *,
    worker: str,
    statuses: List[str],
    claim_status: str,
    ticket_id: Optional[int] = None,
    parent_run_id: str = "",
    task_type: str = "",
    lease_seconds: int = 1800,
) -> Optional[Dict[str, Any]]:
    """Atomically claim one Knowledge run for an AI worker."""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    worker_id = f"{worker}@{socket.gethostname()}"
    payload: Dict[str, Any] = {
        "worker": worker_id,
        "statuses": statuses,
        "claim_status": claim_status,
        "lease_seconds": int(lease_seconds),
    }
    if ticket_id is not None:
        payload["ticket_id"] = int(ticket_id)
    if parent_run_id:
        payload["parent_run_id"] = parent_run_id
    if task_type:
        payload["task_type"] = task_type
    resp = requests.post(
        KNOWLEDGE_API_URL + "/api/runs/worker-claim",
        json=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise RuntimeError(f"Knowledge API POST /api/runs/worker-claim -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    run = data.get("run") if isinstance(data, dict) else None
    if not isinstance(run, dict):
        raise RuntimeError("Knowledge API POST /api/runs/worker-claim returned no run")
    return run


def knowledge_find_requested_run(ticket_id: int) -> Optional[Dict[str, Any]]:
    """同じ Zendesk ticket の未完了 requested run を1件返す。"""
    runs = knowledge_list_runs(ticket_id=ticket_id, status="requested", limit=20)
    return runs[0] if runs else None


def knowledge_get_run(run_id: str) -> Dict[str, Any]:
    """Knowledge API の run 詳細を取得する。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.get(
        KNOWLEDGE_API_URL + f"/api/runs/{run_id}",
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API GET /api/runs/{run_id} -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    run = data.get("run") if isinstance(data, dict) else None
    if not isinstance(run, dict):
        raise RuntimeError(f"Knowledge API GET /api/runs/{run_id} returned no run")
    return run


def knowledge_update_run(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Knowledge API の run を更新する。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.patch(
        KNOWLEDGE_API_URL + f"/api/runs/{run_id}",
        json=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API PATCH /api/runs/{run_id} -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    run = data.get("run") if isinstance(data, dict) else None
    if not isinstance(run, dict):
        raise RuntimeError(f"Knowledge API PATCH /api/runs/{run_id} returned no run")
    return run


def knowledge_attach_run_document(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """既存runへ document を添付する。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.post(
        KNOWLEDGE_API_URL + f"/api/runs/{run_id}/documents",
        json=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API POST /api/runs/{run_id}/documents -> {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def knowledge_list_run_documents(run_id: str, *, include_body: bool = False) -> List[Dict[str, Any]]:
    """Knowledge API の run 添付 document 一覧を取得する。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.get(
        KNOWLEDGE_API_URL + f"/api/runs/{run_id}/documents",
        params={"include_body": "1" if include_body else "0"},
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API GET /api/runs/{run_id}/documents -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    documents = data.get("documents") if isinstance(data, dict) else []
    return documents if isinstance(documents, list) else []


def knowledge_search_documents(query: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Knowledge API の metadata search を実行する。本文全文検索はしない。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.get(
        KNOWLEDGE_API_URL + "/api/search",
        params={"q": query, "limit": int(limit)},
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API GET /api/search -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    documents = data.get("documents") if isinstance(data, dict) else []
    return documents if isinstance(documents, list) else []


def knowledge_get_document(doc_id: str) -> Dict[str, Any]:
    """Knowledge API の document 本文を取得する。"""
    if not KNOWLEDGE_API_URL:
        raise RuntimeError("SUPPORT_AI_KNOWLEDGE_API_URL が未設定です")
    resp = requests.get(
        KNOWLEDGE_API_URL + f"/api/documents/{doc_id}",
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise RuntimeError(f"Knowledge API GET /api/documents/{doc_id} -> {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    document = data.get("document") if isinstance(data, dict) else None
    if not isinstance(document, dict):
        raise RuntimeError(f"Knowledge API GET /api/documents/{doc_id} returned no document")
    return document
