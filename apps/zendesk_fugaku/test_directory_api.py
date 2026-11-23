import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import requests
import relay

CASES = [
    ('GET', 'users.json'), ('GET', 'organizations.json'),
    ('GET', 'organization_memberships.json'), ('GET', 'groups.json'),
    ('GET', 'group_memberships.json'),
    ('GET', 'organizations/search.json'), ('GET', 'organizations/show_many.json'),
    ('GET', 'users/search.json'), ('GET', 'users/show_many.json'),
    ('GET', 'job_statuses/abc-123.json'),
    ('POST', 'organizations/create_many.json'), ('POST', 'users.json'),
    ('POST', 'users/create_many.json'), ('POST', 'organization_memberships.json'),
    ('POST', 'organization_memberships/create_many.json'),
    ('PUT', 'organizations/update_many.json'), ('PUT', 'users/123.json'),
    ('PUT', 'users/update_many.json'), ('PUT', 'users/123/identities/456.json'),
    ('DELETE', 'organizations/destroy_many.json'), ('DELETE', 'users/destroy_many.json'),
]

class DirectoryTests(unittest.TestCase):
    def setUp(self):
        config = SimpleNamespace(zendesk_url='https://example.invalid',
            client_identifier='test', client_secret='test', relay_token='test-token',
            token_bundle_file=Mock(), db_file=Mock())
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"ok":true}'
        response.headers['Content-Type'] = 'application/json'
        with patch.object(relay, 'OAuthClient'), \
                patch.object(relay, 'ZendeskClient') as client, \
                patch.object(relay, 'EventStore'):
            self.app = relay.build_app(config)
            self.upstream = client.return_value.raw_request
            self.upstream.return_value = response
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'}

    def test_all_required_routes_forward_method_query_and_body(self):
        for method, path in CASES:
            with self.subTest(method=method, path=path):
                self.upstream.reset_mock()
                body = b'{"test":"payload"}' if method != 'GET' else None
                r = self.client.open('/api/v2/'+path+'?role%5B%5D=agent&role%5B%5D=admin&page%5Bsize%5D=100',
                    method=method, headers=self.headers, data=body)
                self.assertEqual(r.status_code, 200)
                self.upstream.assert_called_once()
                args, kwargs = self.upstream.call_args
                self.assertEqual(args, (method, '/api/v2/'+path))
                self.assertEqual(kwargs['params'], [('role[]','agent'), ('role[]','admin'), ('page[size]','100')])
                self.assertEqual(kwargs['body'], body)

    def test_every_route_requires_token(self):
        for method,path in CASES:
            with self.subTest(method=method,path=path):
                self.assertEqual(self.client.open('/api/v2/'+path, method=method).status_code,401)
        self.upstream.assert_not_called()

    def test_unsupported_routes_and_methods_never_forward(self):
        for method,path,status in [('DELETE','users/123.json',405),
                ('POST','groups.json',405), ('GET','tickets/123/comments.json',404),
                ('PUT','users/nope.json',404)]:
            self.assertEqual(self.client.open('/api/v2/'+path,method=method,headers=self.headers).status_code,status)
        self.upstream.assert_not_called()

    def test_upstream_status_and_body_are_preserved(self):
        self.upstream.return_value.status_code=422
        self.upstream.return_value._content=b'{"error":"invalid"}'
        r=self.client.get('/api/v2/users.json',headers=self.headers)
        self.assertEqual(r.status_code,422)
        self.assertEqual(r.data,b'{"error":"invalid"}')


class ClientScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.tokens = {'directory': 'a'*43, 'notification': 'b'*43}
        self.source_ips = {'directory': '192.0.2.10', 'notification': '192.0.2.20'}
        self.edge_token = 'e'*43
        policy = {
            'directory': ['GET /api/v2/users.json', 'PUT /api/v2/users/:id.json'],
            'notification': ['POST /api/v2/tickets.json'],
        }
        for name, operations in policy.items():
            token_file = folder / name
            token_file.write_text(self.tokens[name])
            policy[name] = {'token_file': str(token_file), 'source_ip': self.source_ips[name], 'allow': operations}
        self.policy_file = folder / 'policy.json'
        self.policy_file.write_text(json.dumps(policy))
        bundle = folder / 'bundle.json'
        bundle.touch()
        self.config = SimpleNamespace(
            zendesk_url='https://example.invalid', client_identifier='test', client_secret='test',
            relay_token='operator-token', clients_file=str(self.policy_file), token_bundle_file=bundle, db_file=folder / 'events.sqlite',
            edge_token=self.edge_token,
        )
        with patch.object(relay, 'ZendeskClient') as client, patch.object(relay, 'EventStore') as store:
            self.client = relay.build_app(self.config).test_client()
            self.upstream = client.return_value.raw_request
            self.record = store.return_value.record_proxy
            self.upstream.return_value = Mock(ok=True, content=b'{}', status_code=200, headers={})

    def test_tokens_are_scoped_and_labels_come_from_authentication(self):
        for label, method, path, expected in (
            ('directory', 'GET', '/api/v2/users.json', 200),
            ('directory', 'PUT', '/api/v2/users/17.json', 200),
            ('directory', 'POST', '/api/v2/tickets.json', 403),
            ('notification', 'POST', '/api/v2/tickets.json', 200),
            ('notification', 'GET', '/api/v2/users.json', 403),
            ('notification', 'GET', '/events', 401),
        ):
            with self.subTest(label=label, method=method, path=path):
                self.upstream.reset_mock()
                response = self.client.open(path, method=method, headers={
                    'Authorization': 'Bearer ' + self.tokens[label],
                    'X-Fugaku-Edge-Token': self.edge_token,
                    'X-Fugaku-Source-IP': self.source_ips[label],
                    'X-Fugaku-Relay-Client': 'wrong',
                    'X-Request-Id': 'same-id',
                })
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.headers.get('X-Fugaku-Relay-Client'), label if path.startswith('/api/') else None)
                self.assertEqual(self.upstream.call_count, int(expected == 200))
                if expected == 200:
                    self.assertEqual(self.record.call_args.kwargs['event_id'], label + ':same-id')

    def test_operator_token_cannot_call_business_api_in_scoped_mode(self):
        response = self.client.get('/api/v2/users.json', headers={'Authorization': 'Bearer operator-token'})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('X-Fugaku-Relay-Client', response.headers)
        self.upstream.assert_not_called()

    def test_duplicate_tokens_fail_closed(self):
        (self.policy_file.parent / 'notification').write_text(self.tokens['directory'])
        with self.assertRaisesRegex(ValueError, 'Invalid Fugaku relay clients configuration'):
            relay.load_relay_clients(str(self.policy_file))

    def test_wrong_source_or_untrusted_edge_never_forwards(self):
        headers = {
            'Authorization': 'Bearer ' + self.tokens['directory'],
            'X-Fugaku-Source-IP': self.source_ips['directory'],
        }
        for override in (
            {},
            {'X-Fugaku-Edge-Token': self.edge_token, 'X-Fugaku-Source-IP': self.source_ips['notification']},
            {'X-Fugaku-Edge-Token': 'x'*43},
        ):
            with self.subTest(override=override):
                response = self.client.get('/api/v2/users.json', headers=headers | override)
                self.assertEqual(response.status_code, 403)
        self.upstream.assert_not_called()

if __name__=='__main__':
    unittest.main()
