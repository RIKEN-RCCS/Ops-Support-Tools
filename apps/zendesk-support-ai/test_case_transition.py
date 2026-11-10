"""Offline caller tests: no real HTTP, database, identities or credentials."""
import unittest
from unittest.mock import patch

import common
import generator
import followup_responder


class CaseTransitionCallers(unittest.TestCase):
    def invoke(self, module):
        return module._create_knowledge_run(701,{'requires_runbook':True,'summary':'synthetic'},event_key='synthetic-event')

    def test_both_callers_use_shared_atomic_replace(self):
        for module in (generator,followup_responder):
            old={'id':'synthetic-old','case_version':'v','ticket_id':701}
            with patch.object(module,'CREATE_KNOWLEDGE_RUNS',True), patch.object(common,'knowledge_enabled',return_value=True), \
                 patch.object(common,'knowledge_find_case_transition',return_value=None), \
                 patch.object(common,'knowledge_find_requested_run',return_value=old), \
                 patch.object(module.llm_client,'case_decision',return_value={'investigation_decision':'open_new_investigation','case_change':'replace'}), \
                 patch.object(module,'_build_runbook',return_value='synthetic'), \
                 patch.object(common,'knowledge_transition_case',return_value={'run':{'id':'synthetic-new'}}) as transition, \
                 patch.object(common,'knowledge_create_run',side_effect=AssertionError('legacy path forbidden')):
                result=self.invoke(module)
                self.assertEqual(result,('synthetic-new',None,'open_new_investigation','replace'))
                self.assertEqual(transition.call_count,1);self.assertEqual(transition.call_args.args[2],old)

    def test_receipt_recovery_precedes_lookup_and_llm(self):
        for module in (generator,followup_responder):
            with patch.object(module,'CREATE_KNOWLEDGE_RUNS',True), patch.object(common,'knowledge_enabled',return_value=True), \
                 patch.object(common,'knowledge_find_case_transition',return_value={'id':'synthetic-committed'}), \
                 patch.object(common,'knowledge_find_requested_run',side_effect=AssertionError('no current lookup')), \
                 patch.object(module.llm_client,'case_decision',side_effect=AssertionError('no new inference')), \
                 patch.object(common,'knowledge_transition_case',side_effect=AssertionError('no new POST')):
                self.assertEqual(self.invoke(module),('synthetic-committed',None,'attach_to_existing_run','none'))

    def test_deepen_and_broaden_require_operator_policy(self):
        for module in (generator,followup_responder):
            for change in ('deepen','broaden'):
                with patch.object(module,'CREATE_KNOWLEDGE_RUNS',True), patch.object(common,'knowledge_enabled',return_value=True), \
                     patch.object(common,'knowledge_find_case_transition',return_value=None), \
                     patch.object(common,'knowledge_find_requested_run',return_value={'id':'synthetic-old'}), \
                     patch.object(module.llm_client,'case_decision',return_value={'investigation_decision':'open_new_investigation','case_change':change}), \
                     patch.object(module,'_build_runbook',return_value='synthetic'), \
                     patch.object(module,'_attach_decision_document') as document, \
                     patch.object(common,'knowledge_transition_case',side_effect=AssertionError('no new case')):
                    self.assertEqual(self.invoke(module),('synthetic-old',None,'operator_review',change));document.assert_called_once()

    def test_append_context_does_not_create_case(self):
        for module in (generator,followup_responder):
            with patch.object(module,'CREATE_KNOWLEDGE_RUNS',True), patch.object(common,'knowledge_enabled',return_value=True), \
                 patch.object(common,'knowledge_find_case_transition',return_value=None), \
                 patch.object(common,'knowledge_find_requested_run',return_value={'id':'synthetic-old'}), \
                 patch.object(module.llm_client,'case_decision',return_value={'investigation_decision':'attach_to_existing_run','case_change':'append_context'}), \
                 patch.object(module,'_build_runbook',return_value='synthetic'), patch.object(module,'_attach_decision_document'), \
                 patch.object(common,'knowledge_update_run'), \
                 patch.object(common,'knowledge_transition_case',side_effect=AssertionError('no new case')):
                self.assertEqual(self.invoke(module),('synthetic-old',None,'attach_to_existing_run','append_context'))

    def test_ambiguous_parent_selection_stops(self):
        with patch.object(common,'knowledge_list_runs',return_value=[{'id':'first'},{'id':'second'}]):
            with self.assertRaisesRegex(RuntimeError,'ambiguous'):common.knowledge_find_requested_run(701)

    def test_ambiguous_or_incomplete_lookup_does_not_fallback_to_create(self):
        for module in (generator,followup_responder):
            with patch.object(module,'CREATE_KNOWLEDGE_RUNS',True), patch.object(common,'knowledge_enabled',return_value=True), \
                 patch.object(common,'knowledge_find_case_transition',side_effect=RuntimeError('synthetic lookup unavailable')), \
                 patch.object(module,'_build_runbook',return_value='synthetic'), \
                 patch.object(common,'knowledge_transition_case',side_effect=AssertionError('no new POST')):
                self.assertEqual(self.invoke(module)[2],'operator_review')

    def test_post_timeout_is_not_retried(self):
        with patch.object(common.requests,'post',side_effect=common.requests.Timeout('synthetic')), \
             patch.object(common,'_case_transition_headers',return_value={}) as headers:
            with self.assertRaisesRegex(RuntimeError,'outcome_unknown'):
                common.knowledge_transition_case({'ticket_id':701},common.knowledge_case_event_id('triage','synthetic'))
            self.assertEqual(common.requests.post.call_count,1)
            self.assertFalse(common.requests.post.call_args.kwargs['allow_redirects'])

    def test_event_key_is_required_and_source_scoped(self):
        self.assertEqual(common.knowledge_case_event_id('triage','synthetic'),common.knowledge_case_event_id('triage','synthetic'))
        self.assertNotEqual(common.knowledge_case_event_id('triage','synthetic'),common.knowledge_case_event_id('followup','synthetic'))
        with self.assertRaises(ValueError):common.knowledge_case_event_id('followup','')


if __name__=='__main__':unittest.main()
