"""Offline checks for relay transport and administration boundaries."""
import json
import unittest
from unittest.mock import Mock, patch
import requests
import relay
import test_directory_api as fixtures


class SecurityTests(unittest.TestCase):
    setUp = fixtures.ClientScopeTests.setUp

    def headers(self, label='directory'):
        return {'Authorization': 'Bearer '+self.tokens[label],
                'X-Fugaku-Edge-Token': self.edge_token,
                'X-Fugaku-Source-IP': self.source_ips[label]}

    def test_complete_directory_contract_and_notification_contract(self):
        policy = json.loads(self.policy_file.read_text())
        policy['directory']['allow'] = [method+' '+relay.scope_path('/api/v2/'+path)
                                  for method, path in fixtures.CASES]
        notification = [('GET', 'search.json'), ('GET', 'organizations/search.json'),
                        ('POST', 'tickets.json'), ('PUT', 'tickets/123.json'),
                        ('POST', 'uploads.json')]
        policy['notification']['allow'] = [m+' '+relay.scope_path('/api/v2/'+p)
                                             for m, p in notification]
        self.policy_file.write_text(json.dumps(policy))
        with patch.object(relay, 'ZendeskClient') as upstream, patch.object(relay, 'EventStore'):
            upstream.return_value.raw_request.return_value = Mock(
                ok=True, content=b'{}', status_code=200, headers={})
            app = relay.build_app(self.config).test_client()
            for label, cases in [('directory', fixtures.CASES), ('notification', notification)]:
                for method, path in cases:
                    with self.subTest(label=label, method=method, path=path):
                        upstream.return_value.raw_request.reset_mock()
                        result = app.open('/api/v2/'+path, method=method,
                                          headers=self.headers(label), json={'synthetic': True})
                        self.assertEqual(result.status_code, 200)
                        upstream.return_value.raw_request.assert_called_once()

    def test_operator_audit_is_separate_from_business_api(self):
        with patch.object(relay, 'EventStore') as store:
            store.return_value.recent.return_value = []
            app = relay.build_app(self.config).test_client()
            self.assertEqual(app.get('/events', headers={'Authorization': 'Bearer operator-token'}).status_code, 200)
            self.assertEqual(app.get('/events', headers=self.headers()).status_code, 401)
            self.assertEqual(app.get('/api/v2/users.json', headers={'Authorization': 'Bearer operator-token'}).status_code, 401)
        self.upstream.assert_not_called()

    def test_management_routes_and_aliases_cannot_escape_client_policy(self):
        for method, path in [('GET','/api/v2/oauth/tokens.json'),
                             ('GET','/api/v2/guide/theming/themes.json'),
                             ('DELETE','/api/v2/users/123.json'),
                             ('POST','/api/v2/tickets/123.json'),
                             ('PATCH','/api/v2/users/123.json')]:
            with self.subTest(method=method, path=path):
                result = self.client.open(path, method=method, headers=self.headers())
                self.assertIn(result.status_code, (403,404,405))
        self.upstream.assert_not_called()

    def test_rejected_edge_is_not_marked_as_authorized(self):
        headers = self.headers(); headers['X-Fugaku-Edge-Token'] = 'wrong'
        result = self.client.get('/api/v2/users.json', headers=headers)
        self.assertEqual(result.status_code, 403)
        self.assertNotIn('X-Fugaku-Relay-Client', result.headers)
        self.upstream.assert_not_called()

    def test_network_response_loss_is_not_retried_or_logged_verbatim(self):
        self.upstream.side_effect = requests.ConnectionError('SYNTHETIC_PRIVATE_SENTINEL')
        with patch.object(relay, 'log') as log:
            result = self.client.get('/api/v2/users.json', headers=self.headers())
        self.assertEqual(result.status_code, 502)
        self.upstream.assert_called_once()
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', result.get_data(as_text=True))
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', str(self.record.call_args))
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', str(log.call_args))

    def test_upstream_error_body_is_not_persisted(self):
        self.upstream.return_value = Mock(ok=False, status_code=422,
            content=b'{"error":"SYNTHETIC_PRIVATE_SENTINEL"}',
            text='SYNTHETIC_PRIVATE_SENTINEL', headers={})
        result = self.client.get('/api/v2/users.json', headers=self.headers())
        self.assertEqual(result.status_code, 422)
        self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', str(self.record.call_args))

    def test_transport_declines_redirects_for_api_and_oauth(self):
        self.config.timeout = 1
        self.config.scopes = ['read', 'write']
        oauth = Mock(); oauth.ensure_access_token.return_value = 'synthetic'
        api = relay.ZendeskClient(self.config, oauth)
        response = requests.Response(); response.status_code = 302
        response._content = b'{}'; response.headers['Location'] = 'https://other.invalid/'
        with self.client.application.test_request_context('/api/v2/users.json'):
            with patch.object(relay.requests, 'request', return_value=response) as call:
                api.raw_request('GET','/api/v2/users.json')
                self.assertFalse(call.call_args.kwargs['allow_redirects'])
                self.assertEqual(call.call_count, 1)
                api.request('GET','/api/v2/users.json')
                self.assertFalse(call.call_args.kwargs['allow_redirects'])
        response.status_code = 400; response._content = b'SYNTHETIC_PRIVATE_SENTINEL'
        with patch.object(relay.requests, 'post', return_value=response) as call:
            with self.assertRaises(RuntimeError) as error:
                relay.OAuthClient(self.config)._refresh_locked({'refresh_token':'synthetic'})
            self.assertFalse(call.call_args.kwargs['allow_redirects'])
            self.assertNotIn('SYNTHETIC_PRIVATE_SENTINEL', str(error.exception))

    def test_diagnostic_methods_and_security_headers(self):
        for method in ['TRACE','TRACK']:
            result = self.client.open('/health', method=method)
            self.assertEqual(result.status_code, 501)
            self.assertEqual(result.headers['X-Content-Type-Options'], 'nosniff')
        self.upstream.assert_not_called()


if __name__ == '__main__':
    unittest.main()
