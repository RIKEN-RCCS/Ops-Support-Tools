"""Optional authenticated service roles; deployment credentials stay outside Git."""
import hmac
import json
from pathlib import Path
import re
import os
import time
from flask import g, jsonify, request

PROFILES = {'reader', 'triage', 'worker', 'executor', 'operator'}

def install(app, filename, readonly=False):
    clients = {}
    if filename:
        try:
            raw = json.loads(Path(filename).read_text())
            if not isinstance(raw, dict) or not raw:
                raise ValueError()
            seen = set()
            for label, entry in raw.items():
                if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', label):raise ValueError()
                path = Path(entry['token_file']); token = path.read_bytes().strip()
                profile = entry['profile']; worker = entry.get('worker', '')
                if (not path.is_absolute() or not re.fullmatch(rb'[A-Za-z0-9_-]{32,}', token)
                        or token in seen or profile not in PROFILES
                        or (profile == 'worker' and not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', worker))):
                    raise ValueError()
                clients[label] = (token, profile, worker);seen.add(token)
        except (ValueError, OSError, KeyError, TypeError):
            raise ValueError('invalid Knowledge client policy') from None
    app.config['KNOWLEDGE_SCOPED_AUTH'] = bool(clients)

    @app.after_request
    def security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.before_request
    def guard():
        if request.method in {'TRACE', 'TRACK'}:
            return jsonify(ok=False, error='method_not_supported'), 501
        if readonly:
            snapshot = Path(os.environ.get('KNOWLEDGE_DB','/data/db.sqlite'))
            if not snapshot.is_file() or not 0 <= time.time()-snapshot.stat().st_mtime <= 120:
                return jsonify(ok=False, error='view_unavailable'), 503
            if request.path.startswith('/api/') or request.method not in {'GET', 'HEAD'}:
                return jsonify(ok=False, error='read_only_view'), 403
            return None
        if not clients or request.path == '/healthz':return None
        header = request.headers.get('Authorization', '')
        supplied = header[7:].strip().encode() if header.lower().startswith('bearer ') else b''
        entry = next(((label, value) for label, value in clients.items()
                      if supplied and hmac.compare_digest(supplied, value[0])), None)
        if entry is None:return jsonify(ok=False, error='unauthorized'), 401
        label, (_, profile, worker) = entry
        path, method = request.path, request.method
        allowed = profile == 'operator'
        if path.startswith('/api/') and method == 'GET':allowed = True
        if profile == 'triage':
            allowed |= (method == 'POST' and (path == '/api/cases/transitions' or re.fullmatch(r'/api/runs/[^/]+/documents', path) is not None))
            allowed |= method == 'PATCH' and re.fullmatch(r'/api/runs/[^/]+', path) is not None
        if profile == 'worker':
            allowed |= method == 'POST' and (path in {'/api/runs', '/api/documents'} or re.fullmatch(r'/api/runs/[^/]+/documents', path) is not None)
            allowed |= method == 'PATCH' and re.fullmatch(r'/api/runs/[^/]+', path) is not None
            if path == '/api/runs/worker-claim' and method == 'POST':
                payload = request.get_json(silent=True)
                allowed = isinstance(payload, dict) and str(payload.get('worker', '')).split('@', 1)[0] == worker
        if profile == 'triage' and method == 'PATCH':
            payload = request.get_json(silent=True)
            allowed = (allowed and isinstance(payload, dict) and not set(payload)-{'status','summary','issue_on_run'}
                       and ('status' not in payload or payload['status']=='routing_requested'))
        if profile == 'worker':
            payload = request.get_json(silent=True)
            if method == 'POST' and path == '/api/runs':
                allowed = (isinstance(payload, dict) and bool(payload.get('parent_run_id'))
                           and payload.get('task_type') in {'knowledge_research','scope_split','real_machine'}
                           and payload.get('status','requested') in {'requested','routing_requested','split_requested'})
            if method == 'PATCH' and isinstance(payload, dict):
                fields = {'status','summary','issue_on_run'}
                if worker == 'runbook-worker':
                    fields |= {'approval_required','risk_level','executor_mode','required_capabilities'}
                if set(payload)-fields:
                    allowed = False
                if payload.get('status') == 'review_passed' and worker != 'runbook-review-worker':
                    allowed = False
        if profile in {'worker', 'triage'} and method in {'POST', 'PATCH'}:
            payload = request.get_json(silent=True)
            if isinstance(payload, dict) and payload.get('status') in {'executing','result_registered','closed','superseded'}:
                allowed = False
        if profile == 'executor' and method == 'POST':
            allowed |= path == '/api/runs/claim' or re.fullmatch(r'/api/runs/[^/]+/(?:claim/(?:heartbeat|release)|execution-result)', path) is not None
        if not allowed:return jsonify(ok=False, error='forbidden'), 403
        g.knowledge_client = label
        return None
