"""Atomic, idempotent parent-case transitions; no HTTP or worker side effects."""
import hashlib
import json
import time
import uuid

TERMINAL = ('closed', 'superseded', 'done', 'task_done')
SCHEMA = '''CREATE TABLE IF NOT EXISTS case_transition_requests (
 event_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, ticket_id INTEGER NOT NULL,
 mode TEXT NOT NULL, old_case_id TEXT NOT NULL, new_case_id TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL)'''


class Conflict(ValueError):
    pass


def version(row):
    data = {k:v for k,v in dict(row).items() if k not in {'document_count', 'case_version'}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('invalid_case_transition_id')
    return value


def _unclaimed(row):
    return not any(row[k] for k in ('claimed_by', 'claim_token', 'worker_claimed_by', 'worker_claim_token')) and all(
        (row[k] or 0) <= time.time() for k in ('lease_until', 'worker_lease_until'))


def transition(conn, request, prepare, insert, encrypt, decrypt):
    """Caller supplies a private connection; commit only the complete transition."""
    if not isinstance(request, dict) or set(request) != {'event_id', 'mode', 'old_case_id', 'old_case_version', 'run'}:
        raise ValueError('invalid_case_transition_request')
    event_id = _uuid(request['event_id'])
    mode = request['mode']
    payload = request['run']
    if mode not in ('create', 'replace') or not isinstance(payload, dict):
        raise ValueError('invalid_case_transition_request')
    if (type(payload.get('ticket_id')) is not int or payload['ticket_id'] <= 0 or
            payload.get('task_type') != 'investigation_case' or payload.get('parent_run_id') or
            payload.get('status') != 'routing_requested' or 'id' in payload):
        raise ValueError('invalid_case_transition_payload')
    old_id, old_version = request['old_case_id'], request['old_case_version']
    if mode == 'create':
        if old_id != '' or old_version != '':
            raise ValueError('unexpected_previous_case')
    elif not _uuid(old_id) or not isinstance(old_version, str) or len(old_version) != 64:
        raise ValueError('previous_case_version_required')
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'case-transition:' + event_id))
    conn.execute('BEGIN IMMEDIATE')
    try:
        receipt = conn.execute('SELECT * FROM case_transition_requests WHERE event_id=?', (event_id,)).fetchone()
        if receipt:
            if receipt['request_hash'] != request_hash:
                raise Conflict('case_transition_event_conflict')
            existing = conn.execute('SELECT * FROM runs WHERE id=?', (receipt['new_case_id'],)).fetchone()
            if existing is None:
                raise Conflict('case_transition_receipt_inconsistent')
            conn.commit()
            return dict(existing), True
        current = conn.execute("SELECT * FROM runs WHERE ticket_id=? AND task_type='investigation_case' AND parent_run_id='' AND status NOT IN (?,?,?,?)",
                               (payload['ticket_id'], *TERMINAL)).fetchall()
        if mode == 'create':
            if current:
                raise Conflict('current_case_already_exists')
        else:
            if len(current) != 1 or current[0]['id'] != old_id:
                raise Conflict('current_case_changed_or_ambiguous')
            old = current[0]
            if version(old) != old_version or not _unclaimed(old):
                raise Conflict('previous_case_changed_or_claimed')
            children = conn.execute('SELECT * FROM runs WHERE parent_run_id=?', (old_id,)).fetchall()
            if any(c['status'] not in TERMINAL or not _unclaimed(c) for c in children):
                raise Conflict('previous_case_has_active_tasks')
        record = prepare({**payload, 'id': new_id})
        now = int(time.time())
        if mode == 'replace':
            previous = decrypt(old['issue_on_run_ciphertext']) if old['issue_on_run_ciphertext'] else old['issue_on_run']
            note = previous + f'\n\n[case-transition {event_id}] superseded_by: {new_id}; previous_status: {old["status"]}; history retained.'
            conn.execute("UPDATE runs SET status='superseded',issue_on_run='',issue_on_run_ciphertext=?,updated_at=? WHERE id=?",
                         (encrypt(note), now, old_id))
            record['issue_on_run'] = ''
            record['issue_on_run_ciphertext'] = encrypt(str(payload.get('issue_on_run') or '') + f'\n\n[case-transition {event_id}] replaces: {old_id}; history retained.')
        insert(conn, record)
        conn.execute('INSERT INTO case_transition_requests VALUES (?,?,?,?,?,?,?)',
                     (event_id, request_hash, payload['ticket_id'], mode, old_id, new_id, now))
        row = conn.execute('SELECT * FROM runs WHERE id=?', (new_id,)).fetchone()
        conn.commit()
        return dict(row), False
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
