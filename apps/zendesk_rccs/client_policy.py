"""Optional client-scoped Zendesk relay policy, independent of deployment values."""
from dataclasses import dataclass
import hmac
import json
from pathlib import Path
import re


PROFILES = frozenset({"read", "internal-note", "triage", "public-reply", "audit"})
PAGE_KEYS = frozenset({"page", "per_page", "page[size]", "page[after]", "page[before]",
                       "cursor", "sort_by", "sort_order"})
READ_PATHS = {
    "/api/v2/users/me.json": frozenset(),
    "/api/v2/users.json": PAGE_KEYS | {"role", "role[]"},
    "/api/v2/groups.json": PAGE_KEYS,
    "/api/v2/search.json": PAGE_KEYS | {"query"},
}


@dataclass(frozen=True)
class Client:
    token: bytes
    profile: str
    custom_field_ids: frozenset[int]
    assignee_ids: frozenset[int]
    group_ids: frozenset[int]
    public_statuses: frozenset[str]


def ids(value):
    if not isinstance(value, list) or any(type(x) is not int or x <= 0 for x in value):
        raise ValueError("invalid assignment policy")
    return frozenset(value)


def load_clients(filename):
    if not filename:
        return {}
    try:
        entries = json.loads(Path(filename).read_text())
        if not isinstance(entries, dict) or not entries:
            raise ValueError()
        result, tokens = {}, set()
        for label, item in entries.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", label) or label == "legacy":
                raise ValueError()
            if set(item) - {"token_file", "profile", "custom_field_ids", "assignee_ids",
                            "group_ids", "public_statuses"}:
                raise ValueError()
            path = Path(item["token_file"])
            token = path.read_bytes().strip()
            profile = item["profile"]
            statuses = item.get("public_statuses", ["pending"])
            if (not path.is_absolute() or not re.fullmatch(rb"[A-Za-z0-9_-]{32,}", token)
                    or token in tokens or profile not in PROFILES
                    or not isinstance(statuses, list) or not statuses
                    or any(x not in ("new", "open", "pending", "hold") for x in statuses)):
                raise ValueError()
            result[label] = Client(token, profile, ids(item.get("custom_field_ids", [])),
                                   ids(item.get("assignee_ids", [])), ids(item.get("group_ids", [])),
                                   frozenset(statuses))
            tokens.add(token)
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("invalid relay client policy") from None


def authenticate(clients, supplied):
    for label, client in clients.items():
        if hmac.compare_digest(client.token, supplied):
            return label, client
    return None, None


def read_allowed(client, path, query):
    if client.profile == "audit":
        return False
    keys = READ_PATHS.get(path)
    if re.fullmatch(r"/api/v2/tickets/[1-9][0-9]*(?:/comments)?\.json", path):
        keys = PAGE_KEYS if "/comments.json" in path else frozenset()
    if client.profile == "public-reply" and path in (
            "/api/v2/search.json", "/api/v2/users.json", "/api/v2/groups.json"):
        return False
    return keys is not None and set(query) <= keys


def update_allowed(client, payload):
    if client.profile not in {"internal-note", "triage", "public-reply"}:
        return False
    if not isinstance(payload, dict) or set(payload) != {"ticket"}:
        return False
    ticket = payload["ticket"]
    if not isinstance(ticket, dict) or not ticket:
        return False
    allowed = {"comment", "tags"}
    if client.profile == "triage":
        allowed |= {"custom_fields", "assignee_id", "group_id"}
    if client.profile == "public-reply":
        allowed |= {"status"}
    if set(ticket) - allowed:
        return False
    if "comment" in ticket:
        comment = ticket["comment"]
        if (not isinstance(comment, dict) or set(comment) != {"body", "public"}
                or not isinstance(comment["body"], str) or not comment["body"].strip()
                or comment["public"] is not (client.profile == "public-reply")):
            return False
    elif client.profile != "triage":
        return False
    if "tags" in ticket and (not isinstance(ticket["tags"], list)
                             or any(not isinstance(x, str) or not x for x in ticket["tags"])):
        return False
    if "status" in ticket and (not isinstance(ticket["status"], str)
                               or ticket["status"] not in client.public_statuses):
        return False
    for key, pool in (("assignee_id", client.assignee_ids), ("group_id", client.group_ids)):
        if key in ticket and (type(ticket[key]) is not int or ticket[key] not in pool):
            return False
    if "custom_fields" in ticket:
        fields = ticket["custom_fields"]
        if not isinstance(fields, list) or not fields:
            return False
        seen = set()
        for field in fields:
            field_id = field.get("id") if isinstance(field, dict) else None
            if isinstance(field_id, str) and re.fullmatch(r"[1-9][0-9]{0,19}", field_id):
                field_id = int(field_id)
            if (not isinstance(field, dict) or set(field) != {"id", "value"}
                    or type(field_id) is not int or field_id not in client.custom_field_ids
                    or field_id in seen or type(field["value"]) is not int
                    or field["value"] not in client.assignee_ids):
                return False
            seen.add(field_id)
    return True


def allowed(client, method, path, query, payload=None):
    if path == "/events":
        return method == "GET" and client.profile == "audit" and set(query) <= {"limit"}
    if method == "GET":
        return read_allowed(client, path, query)
    return (method == "PUT" and not query
            and re.fullmatch(r"/api/v2/tickets/[1-9][0-9]*\.json", path) is not None
            and update_allowed(client, payload))
