import json
from pathlib import Path
import tempfile
import unittest
from flask import Flask, jsonify
import access_policy

class AccessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.tokens={};policy={}
        for profile in sorted(access_policy.PROFILES):
            token=profile.replace('-','_')+'x'*40;self.tokens[profile]=token
            f=root/profile;f.write_text(token)
            policy[profile]={'token_file':str(f),'profile':profile,'worker':'research-worker'}
        self.policy=root/'clients.json';self.policy.write_text(json.dumps(policy))
        self.app=Flask(__name__);access_policy.install(self.app,str(self.policy))
        self.calls=[]
        @self.app.route('/<path:path>',methods=['GET','POST','PATCH','DELETE'])
        def route(path):self.calls.append(path);return jsonify(ok=True)
        self.client=self.app.test_client()
    def call(self,profile,method,path,payload=None):
        return self.client.open(path,method=method,headers={'Authorization':'Bearer '+self.tokens[profile]},json=payload)
    def test_role_matrix(self):
        cases=[('GET','/api/runs',set(access_policy.PROFILES)),
               ('POST','/api/cases/transitions',{'triage','operator'}),
               ('POST','/api/runs',{'worker','operator'}),
               ('PATCH','/api/runs/one',{'worker','triage','operator'}),
               ('POST','/api/runs/one/documents',{'worker','triage','operator'}),
               ('POST','/api/runs/claim',{'executor','operator'}),
               ('POST','/api/runs/one/execution-result',{'executor','operator'}),
               ('POST','/knowledge/runs/one/operator-action',{'operator'}),
               ('GET','/knowledge',{'operator'})]
        for method,path,profiles in cases:
            for role in self.tokens:
                with self.subTest(role=role,method=method,path=path):
                    before=len(self.calls);r=self.call(role,method,path,{'parent_run_id':'case','task_type':'knowledge_research'} if method=='POST' and path=='/api/runs' else {'status':'routing_requested'} if method=='PATCH' else {})
                    self.assertEqual(r.status_code,200 if role in profiles else 403)
                    self.assertEqual(len(self.calls)-before,int(role in profiles))
    def test_worker_identity_and_execution_boundary(self):
        self.assertEqual(self.call('worker','POST','/api/runs/worker-claim',{'worker':'research-worker@synthetic'}).status_code,200)
        self.assertEqual(self.call('worker','POST','/api/runs/worker-claim',{'worker':'another-worker@synthetic'}).status_code,403)
        self.assertEqual(self.call('reader','POST','/api/runs/worker-claim',{'worker':'research-worker'}).status_code,403)
        self.assertEqual(self.call('worker','POST','/api/runs/one/claim/release',{}).status_code,403)
        self.assertEqual(self.call('worker','PATCH','/api/runs/one',{'status':'result_registered'}).status_code,403)
        self.assertEqual(self.call('worker','POST','/api/runs',{'status':'executing'}).status_code,403)
        self.assertEqual(self.call('worker','POST','/api/runs',{'task_type':'investigation_case'}).status_code,403)
        self.assertEqual(self.call('worker','PATCH','/api/runs/one',{'task_type':'investigation_case'}).status_code,403)
        self.assertEqual(self.call('triage','PATCH','/api/runs/one',{'status':'review_passed'}).status_code,403)
        self.assertEqual(self.call('triage','PATCH','/api/runs/one',{'approval_required':False}).status_code,403)
    def test_missing_and_invalid_credentials(self):
        for h in [{},{'Authorization':'Bearer invalid'}]:self.assertEqual(self.client.get('/api/runs',headers=h).status_code,401)
        self.assertFalse(self.calls)
        result=self.client.open('/healthz',method='TRACE')
        self.assertEqual(result.status_code,501)
        self.assertEqual(result.headers['X-Content-Type-Options'],'nosniff')
    def test_empty_policy_fails_startup(self):
        self.policy.write_text('{}')
        with self.assertRaises(ValueError):access_policy.install(Flask('bad'),str(self.policy))
    def test_readonly_view_cannot_enter_api_or_write(self):
        from unittest.mock import patch
        snapshot=Path(self.tmp.name)/'view.sqlite';snapshot.touch()
        with patch.dict('os.environ',{'KNOWLEDGE_DB':str(snapshot)}):
            a=Flask('view');access_policy.install(a,'',True)
            @a.route('/<path:path>',methods=['GET','POST'])
            def view(path):return 'synthetic'
            c=a.test_client()
            self.assertEqual(c.get('/knowledge').status_code,200)
            self.assertEqual(c.get('/api/runs').status_code,403)
            self.assertEqual(c.post('/knowledge/runs/one/operator-action').status_code,403)
            import os
            os.utime(snapshot,(1,1));self.assertEqual(c.get('/knowledge').status_code,503)
