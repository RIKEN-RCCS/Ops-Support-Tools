"""Offline tests for the live script: no network or real token required."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('live_test', Path(__file__).with_name('test_all_apis.py'))
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.users = {1: {'id':1,'external_id':'production','name':'Existing user','role':'admin'}}
        self.orgs = {2: {'id':2,'external_id':'production','name':'Existing org'}}
        self.jobs = {}
        self.sequence = 10
        self.partial_org_failure = False
        self.fail_delete = False
        self.fail_create_response = False

    def new_id(self):
        self.sequence += 1
        return self.sequence

    def job(self, results):
        ident = 'job-' + str(self.new_id())
        self.jobs[ident] = {'id':ident,'status':'completed','results':results}
        return {'job_status':self.jobs[ident]}

    def request(self, method, path, params=None, body=None):
        self.calls.append((method,path,params,copy.deepcopy(body)))
        p = path[len('/api/v2/'):]
        params = dict(params or {})
        if p.startswith('job_statuses/'):
            return 200, {'job_status':self.jobs[p.split('/')[1][:-5]]}
        plural = p.split('/')[0].replace('.json','')
        records = self.users if plural == 'users' else self.orgs
        if method == 'GET':
            if p in ('users/show_many.json','organizations/show_many.json'):
                external_ids = params['external_ids'].split(',')
                selected = [dict(r) for r in records.values() if r['external_id'] in external_ids]
                data = {plural:selected}
                if plural == 'users':
                    data['identities'] = [{'id':r['id']+1000,'user_id':r['id'],
                        'type':'email','value':r['email']} for r in selected]
                return 200,data
            key = plural if '/' not in p else plural
            return 200,{key:[]}
        if method == 'POST':
            if p == 'users.json':
                user = dict(body['user'],id=self.new_id())
                self.users[user['id']] = user
                if self.fail_create_response:
                    raise live.TestFailure('transport error (write outcome may be unknown)')
                return 201,{'user':dict(user)}
            if 'create_many' in p:
                results=[]
                for index,record in enumerate(body[plural]):
                    if self.partial_org_failure and plural=='organizations' and index==1:
                        results.append({'index':index,'error':'failed'})
                        continue
                    ident=self.new_id()
                    if plural in ('users','organizations'):
                        records[ident]=dict(record,id=ident)
                    results.append({'id':ident,'index':index})
                return 200,self.job(results)
            if p=='organization_memberships.json':
                return 201,{'organization_membership':dict(body['organization_membership'],id=self.new_id())}
        if method=='PUT':
            if p.endswith('/update_many.json'):
                results=[]
                for record in body[plural]:
                    records[record['id']].update(record)
                    results.append({'id':record['id'],'success':True})
                return 200,self.job(results)
            if '/identities/' in p:
                return 200,{'identity':{'id':int(p.split('/')[-1][:-5])}}
            ident=int(p.split('/')[1][:-5])
            self.users[ident].update(body['user'])
            return 200,{'user':dict(self.users[ident])}
        if method=='DELETE':
            if self.fail_delete:raise live.TestFailure('HTTP 403')
            ids=[int(i) for i in params['ids'].split(',')]
            for ident in ids:
                if ident in (1,2):raise AssertionError('touched production data')
                del records[ident]
            return 200,self.job([{'id':i,'success':True} for i in ids])
        raise AssertionError((method,p))


class SuiteTests(unittest.TestCase):
    def test_missing_endpoint_stops_before_reading_token_or_network(self):
        with patch.object(live.API, 'request') as request, patch.dict(live.os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as error:
                live.main(['--token-file', '/not-used'])
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.api=FakeAPI()
        self.state={'run_id':'zrelaytest-20261015-abcdef012345','base_url':'https://relay.example.com','upstream_url':'https://your-subdomain.zendesk.com',
            'users':[],'organizations':[],'pending_jobs':[],'unresolved_creates':{},
            'creation_attempted':False,'cleanup_complete':False}
        self.suite=live.Suite(self.api,self.state,Path(self.tmp.name)/'state.json',0.01,0.001)
        self.addCleanup(patch.stopall)
        patch('sys.stdout',new=io.StringIO()).start()

    def test_full_workflow_and_cleanup_cover_all_required_endpoints(self):
        self.suite.run()
        self.assertTrue(self.suite.cleanup())
        self.assertTrue(all(r['result']=='PASS' for r in self.suite.results))
        self.assertEqual(list(self.api.users),[1])
        self.assertEqual(list(self.api.orgs),[2])
        self.assertEqual(self.state['pending_jobs'],[])
        self.assertEqual(self.state['unresolved_creates'],{})
        calls={(method,path) for method,path,_,_ in self.api.calls}
        required={
            ('GET','/api/v2/users.json'),('GET','/api/v2/organizations.json'),
            ('GET','/api/v2/organization_memberships.json'),('GET','/api/v2/groups.json'),
            ('GET','/api/v2/group_memberships.json'),('GET','/api/v2/organizations/search.json'),
            ('GET','/api/v2/organizations/show_many.json'),('GET','/api/v2/users/search.json'),
            ('GET','/api/v2/users/show_many.json'),('POST','/api/v2/organizations/create_many.json'),
            ('POST','/api/v2/users.json'),('POST','/api/v2/users/create_many.json'),
            ('POST','/api/v2/organization_memberships.json'),
            ('POST','/api/v2/organization_memberships/create_many.json'),
            ('PUT','/api/v2/organizations/update_many.json'),('PUT','/api/v2/users/update_many.json'),
            ('DELETE','/api/v2/organizations/destroy_many.json'),('DELETE','/api/v2/users/destroy_many.json')}
        self.assertTrue(required.issubset(calls))
        self.assertTrue(any('/job_statuses/' in p for _,p in calls))
        self.assertTrue(any('/identities/' in p and m=='PUT' for m,p in calls))
        self.assertTrue(any(m=='PUT' and __import__('re').fullmatch('/api/v2/users/[0-9]+.json',p) for m,p in calls))
        first=self.api.calls[0]
        self.assertIn(('role[]','agent'),first[2])
        self.assertIn(('role[]','admin'),first[2])

    def test_partial_create_failure_still_cleans_created_records(self):
        self.api.partial_org_failure=True
        self.assertIsNone(self.suite.check('org create',self.suite.create_orgs))
        self.assertTrue(self.suite.cleanup())
        self.assertEqual(list(self.api.orgs),[2])

    def test_lost_create_response_reconciles_by_unique_marker(self):
        self.api.fail_create_response=True
        self.suite.check('create user',self.suite.create_single_user)
        self.assertTrue(self.state['unresolved_creates'])
        self.assertTrue(self.suite.cleanup())
        self.assertFalse(self.state['unresolved_creates'])
        self.assertEqual(list(self.api.users),[1])

    def test_unknown_pending_creation_cannot_claim_cleanup_success(self):
        self.state['unresolved_creates']['create organizations']=['org',[0,1]]
        self.assertFalse(self.suite.cleanup())
        self.assertFalse(self.state['cleanup_complete'])
        self.assertFalse(any(m=='DELETE' for m,_,_,_ in self.api.calls))

    def test_delete_failure_retains_state_for_cleanup_only(self):
        self.suite.create_single_user()
        self.api.fail_delete=True
        self.assertFalse(self.suite.cleanup())
        self.assertTrue(self.state['users'])
        self.api.fail_delete=False
        loaded=json.loads(self.suite.state_file.read_text())
        retry=live.Suite(self.api,loaded,self.suite.state_file,0.01,0.001)
        self.assertTrue(retry.cleanup())

    def test_ownership_mismatch_blocks_update_and_delete(self):
        user=self.suite.create_single_user()
        self.api.users[user]['role']='admin'
        with self.assertRaises(live.TestFailure):self.suite.update_single(user)
        self.assertFalse(self.suite.cleanup())
        self.assertFalse(any(m in ('PUT','DELETE') and p.startswith('/api/v2/users')
                             for m,p,_,_ in self.api.calls))

    def test_job_timeout_retains_pending_job_and_does_not_delete(self):
        self.api.jobs['pending']={'status':'working'}
        with self.assertRaises(live.TestFailure):
            self.suite.job({'job_status':{'id':'pending'}},'test job')
        self.assertTrue(self.state['pending_jobs'])
        self.assertFalse(self.suite.cleanup())
        self.assertFalse(any(m=='DELETE' for m,_,_,_ in self.api.calls))

    def test_pagination_is_remapped_without_following_untrusted_origin(self):
        responses=[(200,{'users':[],'links':{'next':'https://your-subdomain.zendesk.com/api/v2/users.json?page%5Bafter%5D=abc'}}),
                   (200,{'users':[]})]
        with patch.object(self.api,'request',side_effect=responses) as request:
            self.suite.list_check('users.json','users')
            self.assertEqual(request.call_args.args[1],'/api/v2/users.json')
            self.assertEqual(request.call_args.args[2],[('page[after]','abc')])
        with patch.object(self.api,'request',return_value=(200,{'users':[],
                'next_page':'https://attacker.invalid/api/v2/users.json'})) as request:
            with self.assertRaises(live.TestFailure):self.suite.list_check('users.json','users')
            self.assertEqual(request.call_count,1)

    def test_token_not_written_and_state_permissions_are_private(self):
        self.suite.save()
        self.assertEqual(self.suite.state_file.stat().st_mode & 0o777,0o600)
        self.assertNotIn('Authorization',self.suite.state_file.read_text())

    def test_cli_has_no_network_when_token_file_is_invalid(self):
        with patch.object(live.API,'request',side_effect=AssertionError('network')):
            with self.assertRaises(SystemExit):live.main(['--token-file',str(Path(self.tmp.name)/'absent'),'--base-url','https://relay.example.com','--upstream-url','https://your-subdomain.zendesk.com'])

    def test_api_rejects_plaintext_and_redirect(self):
        with self.assertRaises(ValueError):live.API('http://example.invalid','secret')
        self.assertIsNone(live.NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.invalid'))

    def test_cli_full_run_writes_report_without_token(self):
        token=Path(self.tmp.name)/'token'
        token.write_text('offline-test-secret\n')
        output=Path(self.tmp.name)/'run'
        with patch.object(live,'API',return_value=self.api), patch.object(live.signal,'signal'):
            code=live.main(['--token-file',str(token),'--output-dir',str(output),'--base-url','https://relay.example.com','--upstream-url','https://your-subdomain.zendesk.com'])
        self.assertEqual(code,0)
        report=json.loads((output/'report.json').read_text())
        self.assertTrue(report['cleanup_complete'])
        self.assertTrue(report['requests'])
        self.assertEqual(output.stat().st_mode & 0o777,0o700)
        self.assertEqual((output/'report.json').stat().st_mode & 0o777,0o600)
        self.assertNotIn('offline-test-secret',(output/'report.json').read_text())
        self.assertNotIn('offline-test-secret',(output/'state.json').read_text())

    def test_cli_interrupt_after_creation_still_runs_cleanup(self):
        token=Path(self.tmp.name)/'token'
        token.write_text('offline-test-secret')
        output=Path(self.tmp.name)/'interrupted'
        def interrupted(suite):
            suite.create_single_user()
            raise KeyboardInterrupt()
        with patch.object(live,'API',return_value=self.api), patch.object(live.signal,'signal'), \
                patch.object(live.Suite,'run',interrupted):
            code=live.main(['--token-file',str(token),'--output-dir',str(output),'--base-url','https://relay.example.com','--upstream-url','https://your-subdomain.zendesk.com'])
        self.assertEqual(code,1)
        self.assertEqual(list(self.api.users),[1])
        report=json.loads((output/'report.json').read_text())
        self.assertTrue(report['interrupted'])
        self.assertTrue(report['cleanup_complete'])


if __name__=='__main__':
    unittest.main()
