"""LLM outage isolation tests; all external API calls are mocked."""
import os
import unittest
from unittest.mock import patch

import startup
import webhook


class WithoutLLMTests(unittest.TestCase):
    def test_webhook_preflight_keeps_zendesk_check_without_llm(self):
        with patch.dict(os.environ, {
            'SUPPORT_AI_STARTUP_CHECKS': '1',
            'SUPPORT_AI_STARTUP_LLM_CHECKS': '0',
            'SUPPORT_AI_SYNC_AGENTS_ON_STARTUP': '0',
        }), patch.object(startup.common, 'ensure_spool_dirs'), \
                patch.object(startup.common, 'check_queue_writable'), \
                patch.object(startup.common, 'ensure_agents_file'), \
                patch.object(startup.common, 'zendesk_healthcheck', return_value={}) as zendesk, \
                patch.object(startup.llm_client, 'healthcheck') as llm:
            startup.preflight()
            zendesk.assert_called_once()
            llm.assert_not_called()

    def test_workers_still_require_llm_by_default(self):
        with patch.dict(os.environ, {
            'SUPPORT_AI_STARTUP_CHECKS': '1',
            'SUPPORT_AI_STARTUP_RETRIES': '1',
        }), patch.object(startup.common, 'ensure_spool_dirs'), \
                patch.object(startup.common, 'check_queue_writable'), \
                patch.object(startup.common, 'ensure_agents_file'), \
                patch.object(startup.common, 'zendesk_healthcheck', return_value={}), \
                patch.object(startup.llm_client, 'healthcheck', side_effect=RuntimeError('offline')):
            os.environ.pop('SUPPORT_AI_STARTUP_LLM_CHECKS', None)
            with self.assertRaisesRegex(RuntimeError, 'llm failed'):
                startup.preflight()

    def test_ai_routes_do_not_enqueue_during_outage(self):
        with patch.dict(os.environ, {'SUPPORT_AI_WEBHOOK_AI_ENABLED': '0'}), \
                patch.object(webhook, '_authorized', return_value=True), \
                patch.object(webhook, 'enqueue_ticket') as triage, \
                patch.object(webhook, 'enqueue_followup') as followup:
            client = webhook.app.test_client()
            for route in ['triage', 'followup']:
                response = client.post('/zendesk/webhook/' + route, json={'ticket_id': 123})
                self.assertEqual(response.status_code, 503)
            triage.assert_not_called()
            followup.assert_not_called()

    def test_ai_routes_resume_enqueuing_when_enabled(self):
        with patch.dict(os.environ, {'SUPPORT_AI_WEBHOOK_AI_ENABLED': '1'}), \
                patch.object(webhook, '_authorized', return_value=True), \
                patch.object(webhook, 'enqueue_ticket', return_value=True) as triage, \
                patch.object(webhook, 'enqueue_followup', return_value=True) as followup:
            client = webhook.app.test_client()
            for route in ['triage', 'followup']:
                response = client.post('/zendesk/webhook/' + route, json={'ticket_id': 123})
                self.assertEqual(response.status_code, 200)
            triage.assert_called_once()
            followup.assert_called_once()

    def test_send_note_still_requires_authentication(self):
        with patch.object(webhook, '_authorized', return_value=False), \
                patch.object(webhook.common, 'fetch_ticket_comments') as fetch:
            response = webhook.app.test_client().post(
                '/zendesk/webhook/send_internal_note', json={'ticket_id': 123})
            self.assertEqual(response.status_code, 401)
            fetch.assert_not_called()

    def test_send_note_works_without_ai_and_strips_marker(self):
        with patch.dict(os.environ, {'SUPPORT_AI_WEBHOOK_AI_ENABLED': '0'}), \
                patch.object(webhook, '_authorized', return_value=True), \
                patch.object(webhook.common, 'fetch_ticket_comments', return_value=[{
                    'id': 456, 'public': False, 'body': '[SEND_INTERNAL_NOTE]\nHuman reply',
                }]), patch.object(webhook.common, 'post_public_reply', return_value={}) as post:
            response = webhook.app.test_client().post(
                '/zendesk/webhook/send_internal_note', json={'ticket_id': 123})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(post.call_args.args, (123, 'Human reply'))


if __name__ == '__main__':
    unittest.main()
