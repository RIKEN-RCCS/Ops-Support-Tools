"""Isolated SQLite + synthetic field encryption; no service or network calls."""
import base64
import concurrent.futures
import copy
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import app as api
import case_lifecycle
import field_crypto


class CaseTransitions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'KNOWLEDGE_FIELD_KEY':base64.urlsafe_b64encode(os.urandom(32)).decode(),
            'KNOWLEDGE_FIELD_KEY_FILE':''})
        self.env.start(); self.addCleanup(self.env.stop)
        self.old_path, self.old_token, self.old_file = api.DB_PATH, api.WRITE_TOKEN, api.WRITE_TOKEN_FILE
        api.DB_PATH = Path(self.tmp.name)/'synthetic.sqlite'
        api.WRITE_TOKEN, api.WRITE_TOKEN_FILE = 'synthetic-write-only', ''
        self.addCleanup(self.restore)
        api._init_db()
        self.client = api.app.test_client()
        self.headers = {'Authorization':'Bearer synthetic-write-only'}
        self.payload = dict(ticket_id=701, task_type='investigation_case', status='routing_requested',
                            runbook='synthetic research', summary='synthetic summary', issue_on_run='synthetic existing issue')
        self.old = api._create_run_record({**self.payload, 'status':'answer_review'})
        self.request = dict(event_id=str(uuid.uuid4()), mode='replace', old_case_id=self.old['id'],
                            old_case_version=self.old['case_version'], run=dict(self.payload))

    def restore(self):
        api.DB_PATH, api.WRITE_TOKEN, api.WRITE_TOKEN_FILE = self.old_path, self.old_token, self.old_file

    def post(self, request=None):
        return self.client.post('/api/cases/transitions', json=request or self.request, headers=self.headers)

    def all_rows(self):
        with api._connect() as c:
            return {t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')]
                    for t in ('runs','documents','run_documents','document_handoffs','case_transition_requests')}

    def test_replace_preserves_history_children_and_documents(self):
        child=api._create_run_record(dict(ticket_id=701,task_type='knowledge_research',parent_run_id=self.old['id'],status='task_done'))
        doc=str(uuid.uuid4())
        with api._connect() as c:
            c.execute('INSERT INTO documents(id,kind,title,path,body_sha256,body_ciphertext,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                (doc,'synthetic','synthetic','synthetic.md','synthetic-hash',field_crypto.encrypt_text('synthetic retained evidence'),1,1))
            c.execute('INSERT INTO run_documents(run_id,document_id,role,created_at) VALUES (?,?,?,?)',(self.old['id'],doc,'evidence',1))
            c.execute('INSERT INTO document_handoffs(id,document_id,note_ciphertext,created_at,updated_at) VALUES (?,?,?,?,?)',
                (str(uuid.uuid4()),doc,field_crypto.encrypt_text('synthetic retained handoff'),1,1))
        before=self.all_rows();r=self.post();self.assertEqual(r.status_code,201)
        data=self.all_rows();old=next(x for x in data['runs'] if x['id']==self.old['id'])
        self.assertEqual(old['status'],'superseded');self.assertEqual(old['runbook_ciphertext'],before['runs'][0]['runbook_ciphertext'])
        self.assertTrue(field_crypto.decrypt_text(old['issue_on_run_ciphertext']).startswith('synthetic existing issue'))
        self.assertIn(r.json['run']['id'],field_crypto.decrypt_text(old['issue_on_run_ciphertext']))
        self.assertIn(self.old['id'],r.json['run']['issue_on_run'])
        self.assertEqual(next(x for x in data['runs'] if x['id']==child['id']),before['runs'][1])
        for t in ('documents','run_documents','document_handoffs'):self.assertEqual(data[t],before[t])
        self.assertEqual(r.json['run']['status'],'routing_requested')

    def test_list_response_version_matches_underlying_parent(self):
        listed=self.client.get('/api/runs?ticket_id=701&task_type=investigation_case').json['runs'][0]
        self.request['old_case_version']=listed['case_version']
        self.assertEqual(self.post().status_code,201)

    def test_new_insert_failure_rolls_back_old_and_receipt(self):
        before=self.all_rows()
        with patch.object(api,'_insert_run_record',side_effect=sqlite3.IntegrityError('synthetic failure')):
            with patch.dict(api.app.config,{'TESTING':True}):
                with self.assertRaises(sqlite3.IntegrityError):self.post()
        self.assertEqual(self.all_rows(),before)

    def test_lost_response_retry_and_receipt_lookup(self):
        first=self.post();second=self.post()
        self.assertEqual((first.status_code,second.status_code),(201,200))
        self.assertEqual(first.json['run']['id'],second.json['run']['id']);self.assertTrue(second.json['replay'])
        self.assertEqual(len(self.all_rows()['runs']),2)
        r=self.client.get('/api/cases/transitions/'+self.request['event_id'],headers=self.headers)
        self.assertEqual(r.json['run']['id'],first.json['run']['id'])
        self.assertNotIn('claim_token',r.json['run'])

    def test_event_id_cannot_be_reused_for_changed_payload(self):
        self.assertEqual(self.post().status_code,201)
        other=copy.deepcopy(self.request);other['run']['summary']='different synthetic summary'
        self.assertEqual(self.post(other).status_code,409)

    def test_stale_version_blocks(self):
        before=self.all_rows();self.request['old_case_version']='0'*64
        self.assertEqual(self.post().status_code,409);self.assertEqual(self.all_rows(),before)

    def test_parent_claim_blocks(self):
        with api._connect() as c:c.execute('UPDATE runs SET worker_claimed_by=? WHERE id=?',('synthetic-worker',self.old['id']))
        self.request['old_case_version']=self.client.get('/api/runs/'+self.old['id']).json['run']['case_version']
        before=self.all_rows();self.assertEqual(self.post().status_code,409);self.assertEqual(self.all_rows(),before)

    def test_active_child_blocks(self):
        api._create_run_record(dict(ticket_id=701,task_type='knowledge_research',parent_run_id=self.old['id'],status='requested'))
        before=self.all_rows();self.assertEqual(self.post().status_code,409);self.assertEqual(self.all_rows(),before)

    def test_ambiguous_current_parents_block(self):
        record=api._prepare_run_record({**self.payload,'status':'answer_review'})
        with api._connect() as c:api._insert_run_record(c,record)
        before=self.all_rows();self.assertEqual(self.post().status_code,409);self.assertEqual(self.all_rows(),before)

    def test_two_concurrent_replacements_have_one_winner(self):
        second={**self.request,'event_id':str(uuid.uuid4())}
        def invoke(request):
            with api.app.test_client() as client:return client.post('/api/cases/transitions',json=request,headers=self.headers).status_code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:codes=list(pool.map(invoke,[self.request,second]))
        self.assertEqual(sorted(codes),[201,409]);self.assertEqual(len(self.all_rows()['case_transition_requests']),1)

    def test_same_event_concurrently_commits_only_once(self):
        def invoke(_):
            with api.app.test_client() as client:
                return client.post('/api/cases/transitions',json=self.request,headers=self.headers).status_code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            codes=list(pool.map(invoke,range(2)))
        self.assertEqual(sorted(codes),[200,201]);self.assertEqual(len(self.all_rows()['runs']),2)

    def test_unexpired_parent_lease_blocks_even_without_claim_name(self):
        with api._connect() as c:c.execute('UPDATE runs SET worker_lease_until=? WHERE id=?',(int(time.time())+60,self.old['id']))
        self.request['old_case_version']=self.client.get('/api/runs/'+self.old['id']).json['run']['case_version']
        before=self.all_rows();self.assertEqual(self.post().status_code,409);self.assertEqual(self.all_rows(),before)

    def test_completed_child_with_claim_still_blocks(self):
        child=api._create_run_record(dict(ticket_id=701,task_type='knowledge_research',parent_run_id=self.old['id'],status='task_done'))
        with api._connect() as c:c.execute('UPDATE runs SET worker_claimed_by=? WHERE id=?',('synthetic-worker',child['id']))
        before=self.all_rows();self.assertEqual(self.post().status_code,409);self.assertEqual(self.all_rows(),before)

    def test_create_is_idempotent_and_guarded(self):
        r=dict(event_id=str(uuid.uuid4()),mode='create',old_case_id='',old_case_version='',run={**self.payload,'ticket_id':702})
        self.assertEqual(self.post(r).status_code,201);self.assertEqual(self.post(r).status_code,200)
        r['event_id']=str(uuid.uuid4());self.assertEqual(self.post(r).status_code,409)

    def test_legacy_create_cannot_bypass_current_parent_guard(self):
        response=self.client.post('/api/runs',json=self.payload)
        self.assertEqual(response.status_code,409);self.assertEqual(len(self.all_rows()['runs']),1)

    def test_cross_ticket_old_case_blocks(self):
        self.request['run']['ticket_id']=702
        self.assertEqual(self.post().status_code,409)

    def test_transition_routes_require_configured_write_token(self):
        self.assertEqual(self.client.post('/api/cases/transitions',json=self.request).status_code,401)
        self.assertEqual(self.client.get('/api/cases/transitions/'+self.request['event_id']).status_code,401)

    def test_invalid_request_does_not_write(self):
        before=self.all_rows();self.request['run']['ticket_id']=True
        self.assertEqual(self.post().status_code,400);self.assertEqual(self.all_rows(),before)


if __name__=='__main__':unittest.main()
