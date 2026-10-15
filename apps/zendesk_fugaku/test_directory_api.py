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

if __name__=='__main__':
    unittest.main()
