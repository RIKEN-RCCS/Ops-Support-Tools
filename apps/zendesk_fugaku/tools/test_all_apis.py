#!/usr/bin/env python3
"""Live integration test. Writes only newly created, uniquely marked test records.

Python 3.8+, standard library only. No requests are made until main() runs.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE = 'https://support.example.com/zendesk_fugaku'


class TestFailure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the Bearer token to a redirect destination.


class API:
    def __init__(self, base, token, timeout=30):
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('base URL must be HTTPS without credentials/query/fragment')
        self.base, self.token, self.timeout = base.rstrip('/'), token, timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, params=None, body=None):
        if not path.startswith('/api/v2/') or '..' in path or '?' in path:
            raise ValueError('invalid API path')
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params, doseq=True)
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Do not log response bodies, URLs with query data, or credentials.
            raise TestFailure('HTTP ' + str(exc.code)) from None
        except (OSError, urllib.error.URLError):
            raise TestFailure('transport error (write outcome may be unknown)') from None
        try:
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise TestFailure('non-object JSON response') from None
        return status, result


class Suite:
    def __init__(self, api, state, state_file, job_timeout=180, poll_interval=2):
        self.api, self.state, self.state_file = api, state, Path(state_file)
        self.job_timeout, self.poll_interval = job_timeout, poll_interval
        self.results = []
        self.requests = []

    def save(self):
        temporary = self.state_file.with_suffix('.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.state, stream, indent=2)
            stream.write('\n')
        os.replace(str(temporary), str(self.state_file))

    def check(self, label, function):
        try:
            value = function()
            self.results.append({'test': label, 'result': 'PASS'})
            print('PASS ' + label, flush=True)
            return value
        except Exception as exc:
            reason = str(exc) if isinstance(exc, TestFailure) else type(exc).__name__
            self.results.append({'test': label, 'result': 'FAIL', 'reason': reason})
            print('FAIL ' + label + ': ' + reason, flush=True)
            return None

    def skip(self, label):
        self.results.append({'test': label, 'result': 'SKIP', 'reason': 'prerequisite failed'})
        print('SKIP ' + label, flush=True)

    def call(self, method, path, params=None, body=None):
        try:
            status, data = self.api.request(method, '/api/v2/' + path, params, body)
        except TestFailure as exc:
            match = re.fullmatch(r'HTTP ([0-9]{3})', str(exc))
            self.requests.append({'method':method,'path':path,
                                  'http':int(match[1]) if match else None})
            raise
        self.requests.append({'method':method,'path':path,'http':status})
        print('  {} {} HTTP {}'.format(method, path, status), flush=True)
        return data

    def own_external(self, kind, index):
        return '{}-{}-{}'.format(self.state['run_id'], kind, index)

    def validate(self, kind, record):
        prefix = self.state['run_id'] + '-' + kind + '-'
        external = record.get('external_id') or ''
        index = external[len(prefix):] if external.startswith(prefix) else ''
        if (index not in ('0', '1') or record.get('external_id') != self.own_external(kind, index)
                or not record.get('name', '').startswith(self.state['run_id'] + '-')
                or (kind == 'user' and record.get('role') != 'end-user')):
            raise TestFailure('ownership marker mismatch; refusing modification')
        ident = record.get('id')
        if not isinstance(ident, int) or ident <= 0:
            raise TestFailure('invalid resource ID')
        return ident

    def fetch_owned(self, kind):
        plural = 'users' if kind == 'user' else 'organizations'
        params = {'external_ids': ','.join(self.own_external(kind, i) for i in range(2))}
        if kind == 'user':
            params['include'] = 'identities'
        data = self.call('GET', plural + '/show_many.json', params)
        records = data.get(plural)
        if not isinstance(records, list):
            raise TestFailure('missing ' + plural + ' array')
        owned = {}
        for record in records:
            ident = self.validate(kind, record)
            owned[ident] = record
        self.state[plural] = list(owned)
        self.save()
        return owned, data

    def job(self, data, label, expected=None):
        job = data.get('job_status')
        if not isinstance(job, dict) or not re.fullmatch(r'[A-Za-z0-9_-]+', str(job.get('id', ''))):
            raise TestFailure('missing job ID')
        entry = {'id': str(job['id']), 'label': label, 'expected': expected}
        self.state['pending_jobs'].append(entry)
        self.state['unresolved_creates'].pop(label, None)
        self.save()  # Persist before polling; a timeout must not lose the job.
        return self.wait_job(entry)

    def wait_job(self, entry):
        deadline = time.monotonic() + self.job_timeout
        while True:
            data = self.call('GET', 'job_statuses/' + entry['id'] + '.json')
            job = data.get('job_status', {})
            if job.get('status') in ('completed', 'failed', 'killed'):
                self.state['pending_jobs'].remove(entry)
                self.save()
                results = job.get('results')
                if job['status'] != 'completed' or not isinstance(results, list):
                    raise TestFailure('asynchronous job did not complete successfully')
                if entry.get('expected') is not None and len(results) != entry['expected']:
                    raise TestFailure('asynchronous job result count mismatch')
                if any(r.get('error') or r.get('errors') or r.get('success') is False
                       or str(r.get('status', '')).lower() in ('failed', 'error') for r in results):
                    raise TestFailure('asynchronous job contains failed items')
                return results
            if time.monotonic() >= deadline:
                raise TestFailure('job timeout; state retained for cleanup-only')
            time.sleep(self.poll_interval)

    def list_check(self, path, key, params=None):
        data = self.call('GET', path, params or {'page[size]': 1})
        if not isinstance(data.get(key), list):
            raise TestFailure('expected list missing')
        # Check at most one additional page, never follow a foreign absolute URL.
        nxt = data.get('links', {}).get('next') or data.get('next_page')
        if nxt:
            parsed = urllib.parse.urlsplit(nxt)
            api_path = '/api/v2/' + path
            if (parsed.scheme != 'https' or parsed.netloc != 'your-subdomain.zendesk.com'
                    or parsed.path not in (api_path, api_path[:-5])):
                raise TestFailure('unexpected pagination URL; not followed')
            second = self.call('GET', path, urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            if not isinstance(second.get(key), list):
                raise TestFailure('expected list missing on second page')

    def create_orgs(self):
        self.state['creation_attempted'] = True
        self.state['unresolved_creates']['create organizations'] = ['org', [0, 1]]
        self.save()
        payload = [{'name': self.own_external('org', i),
                    'external_id': self.own_external('org', i),
                    'tags': [self.state['run_id']], 'domain_names': []} for i in range(2)]
        self.job(self.call('POST', 'organizations/create_many.json', body={'organizations': payload}),
                 'create organizations', 2)
        owned, _ = self.fetch_owned('org')
        if len(owned) != 2:
            raise TestFailure('expected two marked organizations')
        return sorted(owned)

    def user_payload(self, index):
        name = self.own_external('user', index)
        return {'name': name, 'external_id': name, 'email': name + '@example.com',
                'role': 'end-user', 'skip_verify_email': True, 'tags': [self.state['run_id']]}

    def create_single_user(self):
        self.state['creation_attempted'] = True
        self.state['unresolved_creates']['create single user'] = ['user', [0]]
        self.save()
        data = self.call('POST', 'users.json', body={'user': self.user_payload(0)})
        user = data.get('user', {})
        ident = self.validate('user', user)
        self.state['users'].append(ident)
        self.state['unresolved_creates'].pop('create single user', None)
        self.save()
        return ident

    def create_bulk_user(self):
        self.state['creation_attempted'] = True
        self.state['unresolved_creates']['create bulk user'] = ['user', [1]]
        self.save()
        self.job(self.call('POST', 'users/create_many.json', body={'users': [self.user_payload(1)]}),
                 'create bulk user', 1)
        owned, _ = self.fetch_owned('user')
        found = [ident for ident, record in owned.items()
                 if record['external_id'] == self.own_external('user', 1)]
        if len(found) != 1:
            raise TestFailure('bulk-created user not found')
        return found[0]

    def assert_owned(self, kind, ids):
        owned, _ = self.fetch_owned(kind)
        if not ids or not set(ids).issubset(owned):
            raise TestFailure('test-owned resources missing; refusing modification')

    def update_single(self, user):
        self.assert_owned('user', [user])
        data = self.call('PUT', 'users/' + str(user) + '.json',
                         body={'user': {'notes': self.state['run_id'] + '-single'}})
        if data.get('user', {}).get('notes') != self.state['run_id'] + '-single':
            raise TestFailure('user update not reflected')

    def update_many(self, kind, ids):
        self.assert_owned(kind, ids)
        plural = 'users' if kind == 'user' else 'organizations'
        note = self.state['run_id'] + '-bulk'
        self.job(self.call('PUT', plural + '/update_many.json',
                 body={plural: [{'id': i, 'notes': note} for i in ids]}), 'update ' + plural, len(ids))
        owned, _ = self.fetch_owned(kind)
        if any(owned[i].get('notes') != note for i in ids):
            raise TestFailure('bulk update not reflected')

    def membership(self, user, org, bulk=False):
        self.assert_owned('user', [user])
        self.assert_owned('org', [org])
        payload = {'user_id': user, 'organization_id': org}
        if bulk:
            self.job(self.call('POST', 'organization_memberships/create_many.json',
                     body={'organization_memberships': [payload]}), 'create memberships', 1)
        else:
            record = self.call('POST', 'organization_memberships.json',
                               body={'organization_membership': payload}).get('organization_membership', {})
            if record.get('user_id') != user or record.get('organization_id') != org:
                raise TestFailure('membership result mismatch')

    def identity(self, user):
        owned, data = self.fetch_owned('user')
        if user not in owned:
            raise TestFailure('test user missing')
        identities = data.get('identities', [])
        matches = [i for i in identities if i.get('user_id') == user and i.get('type') == 'email'
                   and i.get('value') == self.own_external('user', 0) + '@example.com']
        if len(matches) != 1 or not isinstance(matches[0].get('id'), int):
            raise TestFailure('test identity not found in sideload')
        data = self.call('PUT', 'users/{}/identities/{}.json'.format(user, matches[0]['id']),
                         body={'identity': {'verification_method': 'none'}})
        if data.get('identity', {}).get('id') != matches[0]['id']:
            raise TestFailure('identity response mismatch')

    def run(self):
        for label, path, key, params in [
            ('list agents/admins', 'users.json', 'users', [('role[]','agent'),('role[]','admin'),('page[size]',1)]),
            ('list end users', 'users.json', 'users', {'role[]':'end-user','page[size]':1}),
            ('list organizations', 'organizations.json', 'organizations', None),
            ('list organization memberships', 'organization_memberships.json', 'organization_memberships', None),
            ('list groups', 'groups.json', 'groups', None),
            ('list group memberships', 'group_memberships.json', 'group_memberships', None),
        ]:
            self.check(label, lambda p=path,k=key,q=params: self.list_check(p,k,q))
        orgs = self.check('POST organizations/create_many + job_status', self.create_orgs)
        user = self.check('POST users', self.create_single_user)
        bulk_user = self.check('POST users/create_many + job_status', self.create_bulk_user)
        for kind in ('org', 'user'):
            self.check('GET ' + kind + ' show_many', lambda k=kind: self.fetch_owned(k))
        self.check('GET organizations/search', lambda: self.list_check(
            'organizations/search.json', 'organizations', {'name':self.own_external('org',0)}))
        self.check('GET users/search', lambda: self.list_check(
            'users/search.json', 'users', {'query': self.own_external('user',0)+'@example.com'}))
        for label, ready, function in [
            ('POST organization_memberships', user and orgs,
             lambda: self.membership(user,orgs[0])),
            ('POST organization_memberships/create_many + job_status', bulk_user and orgs,
             lambda: self.membership(bulk_user,orgs[1],True)),
            ('PUT users/{id}', user, lambda: self.update_single(user)),
            ('PUT users/update_many + job_status', user and bulk_user,
             lambda: self.update_many('user',[user,bulk_user])),
            ('PUT organizations/update_many + job_status', orgs,
             lambda: self.update_many('org',orgs)),
            ('PUT users/{id}/identities/{id}', user, lambda: self.identity(user)),
        ]:
            self.check(label,function) if ready else self.skip(label)

    def cleanup(self):
        # Wait for already-submitted creations/updates before trying to delete.
        for entry in list(self.state['pending_jobs']):
            self.check('cleanup settle ' + entry['label'], lambda e=entry: self.wait_job(e))
        if self.state['pending_jobs']:
            self.skip('cleanup (pending job could create more resources)')
            return False
        # A lost create response may hide a job that is still queued. Do not
        # claim successful cleanup just because no objects are visible yet.
        for label, (kind, indexes) in list(self.state['unresolved_creates'].items()):
            def reconcile(k=kind, expected=indexes, name=label):
                owned, _ = self.fetch_owned(k)
                found = {r['external_id'] for r in owned.values()}
                if not all(self.own_external(k, i) in found for i in expected):
                    raise TestFailure('create outcome unknown; wait and rerun cleanup-only')
                self.state['unresolved_creates'].pop(name)
                self.save()
                return True
            self.check('cleanup reconcile '+label, reconcile)
        unresolved = bool(self.state['unresolved_creates'])
        if unresolved:
            self.skip('cleanup (unknown create outcome; retain state and retry later)')
            return False
        success = True
        for kind in ('user','org'):
            plural = 'users' if kind == 'user' else 'organizations'
            def destroy(k=kind,p=plural):
                owned, _ = self.fetch_owned(k)
                if owned:
                    ids = sorted(owned)
                    self.job(self.call('DELETE',p+'/destroy_many.json',{'ids':','.join(map(str,ids))}),
                             'delete '+p,len(ids))
                # Re-read by exact unique external_ids, checking user soft-delete.
                remaining, _ = self.fetch_owned(k)
                active = [i for i,r in remaining.items() if not r.get('deleted',False)]
                if active:
                    raise TestFailure('cleanup left active test resources')
                self.state[p] = []
                self.save()
                return True
            ok = self.check('DELETE '+plural+'/destroy_many + verify cleanup',destroy)
            success = bool(ok) and success
            if self.state['pending_jobs']:
                return False
        success = success and not unresolved
        self.state['cleanup_complete'] = success
        self.save()
        return success


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--base-url', default=None)
    parser.add_argument('--output-dir', type=Path, help='new private directory (must not exist)')
    parser.add_argument('--cleanup-only', type=Path, metavar='STATE_JSON')
    parser.add_argument('--job-timeout', type=float, default=180)
    parser.add_argument('--poll-interval', type=float, default=2)
    args = parser.parse_args(argv)
    if args.job_timeout <= 0 or args.poll_interval <= 0:
        parser.error('timeouts must be positive')
    try:
        token = args.token_file.read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise ValueError()
    except (OSError, ValueError):
        parser.error('cannot read a nonempty, single-line token file')
    if args.cleanup_only:
        state_file = args.cleanup_only.resolve()
        state = json.loads(state_file.read_text())
        if not re.fullmatch(r'zrelaytest-[0-9]{8}-[a-f0-9]{12}', state.get('run_id','')):
            parser.error('invalid state run ID')
        if args.base_url and args.base_url.rstrip('/') != state['base_url']:
            parser.error('cleanup base URL must match saved state')
        base = state['base_url']
    else:
        base = (args.base_url or DEFAULT_BASE).rstrip('/')
        run_id = 'zrelaytest-' + datetime.datetime.now().strftime('%Y%m%d') + '-' + uuid.uuid4().hex[:12]
        directory = (args.output_dir or Path(run_id)).resolve()
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        state_file = directory/'state.json'
        state = {'run_id':run_id,'base_url':base,'users':[],'organizations':[],
                 'pending_jobs':[],'unresolved_creates':{},'creation_attempted':False,'cleanup_complete':False}
    suite = Suite(API(base,token),state,state_file,args.job_timeout,args.poll_interval)
    suite.save()
    interrupted = False
    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    try:
        if not args.cleanup_only:
            suite.run()
    except KeyboardInterrupt:
        interrupted = True
        print('Interrupted; attempting cleanup.',flush=True)
    finally:
        try:
            cleaned = suite.cleanup()
        except (Exception, KeyboardInterrupt):
            cleaned = False
            print('Cleanup interrupted/failed; use saved state.',flush=True)
        report = {'run_id':state['run_id'],'base_url':base,'tests':suite.results,
                  'requests':suite.requests,'cleanup_complete':cleaned,'interrupted':interrupted}
        report_file = state_file.parent/'report.json'
        fd = os.open(str(report_file),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as stream:
            json.dump(report,stream,indent=2)
        print('Report: '+str(report_file))
        print('Cleanup: '+('complete' if cleaned else 'INCOMPLETE; state: '+str(state_file)))
    return 0 if cleaned and not interrupted and all(r['result']=='PASS' for r in suite.results) else 1


if __name__ == '__main__':
    sys.exit(main())
