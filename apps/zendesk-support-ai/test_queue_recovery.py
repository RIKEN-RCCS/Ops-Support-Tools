import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import common
import generator
import poster
import followup
import followup_responder


class RecoveryTests(unittest.TestCase):
    def test_connection_context_closes_and_probe_changes_no_rows(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(common, 'QUEUE_DB', Path(temp)/'queue.sqlite'):
            common.check_queue_writable()
            with common._queue_conn() as conn:
                self.assertEqual(conn.execute('select count(*) from queue_items').fetchone()[0],0)
            with self.assertRaises(sqlite3.ProgrammingError):conn.execute('select 1')

    def test_readonly_failure_precedes_llm_and_case_creation(self):
        with patch.object(common,'read_json',return_value={'ticket_id':42}), \
                patch.object(common,'check_queue_writable',side_effect=sqlite3.OperationalError('readonly')), \
                patch.object(common,'move_to'), patch.object(generator,'_extract_texts') as fetch, \
                patch.object(generator,'_create_knowledge_run') as create:
            self.assertFalse(generator.process_one(Mock()))
            fetch.assert_not_called();create.assert_not_called()

    def test_existing_active_case_lookup_does_not_filter_requested_only(self):
        cases=[{'id':'child','parent_run_id':'p','status':'requested'},
               {'id':'closed','status':'closed'}, {'id':'active','status':'answer_review'}]
        with patch.object(common,'knowledge_list_runs',return_value=cases) as listing:
            self.assertEqual(common.knowledge_find_requested_run(42)['id'],'active')
            self.assertEqual(listing.call_args.kwargs['task_type'],'investigation_case')
            self.assertNotIn('status',listing.call_args.kwargs)

    def test_posting_workers_stop_before_side_effects_if_queue_is_readonly(self):
        for module in [poster,followup,followup_responder]:
            with self.subTest(worker=module.__name__), patch.object(common,'ensure_spool_dirs'), \
                    patch.object(common,'check_queue_writable',side_effect=sqlite3.OperationalError('readonly')), \
                    patch.object(common,'list_queue') as listing:
                with self.assertRaises(sqlite3.OperationalError):
                    module.run_once(dry_run=False) if module is poster else module.run_once()
                listing.assert_not_called()

    def test_retry_reuses_event_case_even_after_state_changed(self):
        seen={}
        def create(payload):
            seen[payload['id']]=dict(payload)
            return {'run':seen[payload['id']]}
        with patch.object(generator,'CREATE_KNOWLEDGE_RUNS',True), \
                patch.object(common,'knowledge_enabled',return_value=True), \
                patch.object(generator,'_build_runbook',return_value='test'), \
                patch.object(common,'knowledge_find_requested_run',return_value=None), \
                patch.object(common,'knowledge_find_run',side_effect=lambda ident:seen.get(ident)), \
                patch.object(common,'knowledge_create_run',side_effect=create) as post:
            first=generator._create_knowledge_run(42,{'requires_runbook':True},event_key='42:event:1')
            seen[first[0]]['status']='answer_review'
            second=generator._create_knowledge_run(42,{'requires_runbook':True},event_key='42:event:1')
            self.assertEqual(first[0],second[0]);self.assertIsNone(second[1]);post.assert_called_once()
            third=generator._create_knowledge_run(42,{'requires_runbook':True},event_key='42:event:2')
            self.assertNotEqual(first[0],third[0])

    def test_commit_then_timeout_recovers_existing_case(self):
        seen={}
        def create(payload):
            seen[payload['id']]=dict(payload)
            raise TimeoutError('response lost')
        with patch.object(generator,'CREATE_KNOWLEDGE_RUNS',True), \
                patch.object(common,'knowledge_enabled',return_value=True), \
                patch.object(generator,'_build_runbook',return_value='test'), \
                patch.object(common,'knowledge_find_requested_run',return_value=None), \
                patch.object(common,'knowledge_find_run',side_effect=lambda ident:seen.get(ident)), \
                patch.object(common,'knowledge_create_run',side_effect=create):
            result=generator._create_knowledge_run(42,{'requires_runbook':True},event_key='42:event:1')
            self.assertIsNone(result[1]);self.assertEqual(len(seen),1)


if __name__=='__main__':unittest.main()
