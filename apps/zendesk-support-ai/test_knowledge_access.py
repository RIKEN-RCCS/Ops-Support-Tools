import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
import common
import research_startup

class KnowledgeAccessTests(unittest.TestCase):
    def test_all_common_calls_carry_client_credential_without_redirect(self):
        response=Mock(ok=True,status_code=200)
        response.json.return_value={'run':{'id':'synthetic','ticket_id':7},'runs':[],'documents':[],'document':{'id':'synthetic'}}
        with patch.dict(os.environ,{'SUPPORT_AI_KNOWLEDGE_TOKEN':'synthetic-client','SUPPORT_AI_KNOWLEDGE_TOKEN_FILE':''}),patch.object(common,'KNOWLEDGE_API_URL','http://synthetic.invalid'),patch.object(common.requests,'get',return_value=response) as get,patch.object(common.requests,'post',return_value=response) as post,patch.object(common.requests,'patch',return_value=response) as modify:
            common.knowledge_list_runs();common.knowledge_get_run('synthetic');common.knowledge_find_run('synthetic')
            common.knowledge_create_run({});common.knowledge_update_run('synthetic',{'summary':'synthetic'})
            common.knowledge_attach_run_document('synthetic',{});common.knowledge_list_run_documents('synthetic')
            common.knowledge_search_documents('synthetic');common.knowledge_get_document('synthetic')
            common.knowledge_worker_claim_run(worker='research-worker',statuses=['requested'],claim_status='planning')
            for request in [*get.call_args_list,*post.call_args_list,*modify.call_args_list]:
                self.assertEqual(request.kwargs['headers']['Authorization'],'Bearer synthetic-client')
                self.assertFalse(request.kwargs['allow_redirects'])
    def test_configured_empty_credential_never_falls_back_to_operator(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'empty';p.touch()
            with patch.dict(os.environ,{'SUPPORT_AI_KNOWLEDGE_TOKEN_FILE':str(p),'KNOWLEDGE_API_WRITE_TOKEN':'synthetic-operator'}):
                with self.assertRaises(RuntimeError):common._case_transition_headers()
    def test_startup_never_opens_queue_or_syncs_agents(self):
        with patch.object(sys,'argv',['research_startup.py','python','synthetic_worker.py']),patch.object(common,'knowledge_enabled',return_value=True),patch.object(common,'env_secret',return_value='synthetic'),patch.object(common,'knowledge_list_runs',return_value=[]),patch.object(common,'check_queue_writable',side_effect=AssertionError('queue access')),patch.object(common,'sync_agents_config',side_effect=AssertionError('agent config access')),patch.object(os,'execvp') as execute:
            research_startup.main();execute.assert_called_once_with('python',['python','synthetic_worker.py'])
