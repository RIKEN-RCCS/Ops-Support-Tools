import sys,json,tempfile,dataclasses
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import relay,client_policy
import unittest
count = 0


class ClientPolicyTest(unittest.TestCase):
    def test_scoped_proxy_and_actual_callers(self):
        global count
        count = 0
        with tempfile.TemporaryDirectory() as root:
         p=Path(root); entries={}
         for profile in sorted(client_policy.PROFILES):
          f=p/profile;f.write_text(profile.replace('-','_')+'_'*40)
          entries[profile]={'token_file':str(f),'profile':profile,'custom_field_ids':[123],'assignee_ids':[456],'group_ids':[789]}
         policy=p/'clients.json';policy.write_text(json.dumps(entries));bundle=p/'oauth.json';bundle.write_text('{}')
         config=relay.Config('https://mock.invalid','mock','mock',bundle,[], 'legacy_'+'x'*40,p/'events.sqlite',60,0,False,[],'',1,str(policy))
         app=relay.build_app(config);app.testing=True;c=app.test_client()
         def response(*a,**kw):
          import requests
          r=requests.Response();r.status_code=200;r._content=b'{"ok":true}';r.headers['Content-Type']='application/json';return r
         with patch.object(relay.OAuthClient,'ensure_access_token',return_value='synthetic-oauth') as oauth,patch.object(relay.requests,'request',side_effect=response) as upstream:
          def check(profile,method,path,status,payload=None):
           global count
           token=(p/profile).read_text() if profile in entries else profile
           before=upstream.call_count;obefore=oauth.call_count
           r=c.open(path,method=method,headers={'Authorization':'Bearer '+token},json=payload)
           assert r.status_code==status,(profile,method,path,r.status_code,r.data)
           if status in (401,403,501):assert upstream.call_count==before and oauth.call_count==obefore
           assert r.headers['X-Content-Type-Options']=='nosniff'
           count+=1;return r
          for profile in entries:
           for path in ['/api/v2/users/me.json','/api/v2/tickets/42.json','/api/v2/tickets/42/comments.json?page=2']:
            check(profile,'GET',path,403 if profile=='audit' else 200)
           for path in ['/api/v2/users.json?role%5B%5D=agent&role%5B%5D=admin&page=2','/api/v2/groups.json','/api/v2/search.json?query=type%3Aticket&page=2']:
            check(profile,'GET',path,403 if profile in ('audit','public-reply') else 200)
           check(profile,'GET','/events',200 if profile=='audit' else 403)
           for method,path,payload in [('DELETE','/api/v2/tickets/42.json',None),('POST','/api/v2/tickets.json',{}),('POST','/api/v2/tickets/42.json',{}),('PATCH','/api/v2/tickets/42.json',{}),('GET','/api/v2/users/42.json',None),('GET','/api/v2/tickets/42.json?include=users',None),('HEAD','/api/v2/tickets/42.json',None),('OPTIONS','/api/v2/tickets/42.json',None),('GET','/api/v2/../users.json',None)]:
            check(profile,method,path,403,payload)
           for public in (False,True):
            permitted=(profile=='public-reply' if public else profile in ('internal-note','triage'))
            check(profile,'PUT','/api/v2/tickets/42.json',200 if permitted else 403,{'ticket':{'comment':{'body':'synthetic','public':public},'tags':['synthetic']}})
           for field,value in [('assignee_id',456),('group_id',789),('custom_fields',[{'id':123,'value':456}])]:
            check(profile,'PUT','/api/v2/tickets/42.json',200 if profile=='triage' else 403,{'ticket':{field:value}})
          for payload in [{'ticket':{'assignee_id':999}},{'ticket':{'assignee_id':True}},{'ticket':{'custom_fields':[{'id':999,'value':456}]}},{'ticket':{'comment':{'body':'x','public':False,'author_id':456}}},{'ticket':{'status':'solved'}},{'ticket':{'subject':'x'}},{'ticket':{'comment':{'body':'x','public':'false'}}}]:
           check('triage','PUT','/api/v2/tickets/42.json',403,payload)
          for field_id, expected in [('123', 200), ('999', 403), ('123.0', 403)]:
           check('triage','PUT','/api/v2/tickets/42.json',expected,{'ticket':{'custom_fields':[{'id':field_id,'value':456}]}})
          check('public-reply','PUT','/api/v2/tickets/42.json',200,{'ticket':{'comment':{'body':'synthetic','public':True},'status':'pending'}})
          check(config.relay_token,'GET','/api/v2/users/me.json',401)
          check('incorrect','GET','/api/v2/users/me.json',401)
          check('read','TRACE','/api/v2/users/me.json',501)
          check('read','GET','/api/v2/users.json?role%5B%5D=agent&role%5B%5D=admin',200)
          assert upstream.call_args.kwargs['params']==[('role[]','agent'),('role[]','admin')]
          assert upstream.call_args.kwargs['allow_redirects'] is False;count+=2
          import requests
          upstream.side_effect=requests.ConnectionError('PRIVATE SENTINEL')
          before=upstream.call_count
          r=check('internal-note','PUT','/api/v2/tickets/42.json',502,{'ticket':{'comment':{'body':'synthetic','public':False}}})
          assert upstream.call_count==before+1 and b'PRIVATE SENTINEL' not in r.data;count+=1
          assert 'PRIVATE SENTINEL' not in json.dumps(c.get('/events',headers={'Authorization':'Bearer '+(p/'audit').read_text()}).json);count+=1
          upstream.side_effect=response
          # Execute the actual caller functions without importing their startup/queue code.
          import ast
          tree=ast.parse((Path(__file__).resolve().parent.parent / 'zendesk-support-ai' / 'common.py').read_text())
          names={'fetch_ticket','fetch_ticket_comments','list_users','list_groups','set_assignee_field','set_ticket_assignee','post_internal_note','post_public_reply'}
          selected=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[])
          selected_profile='triage'
          def dispatch(method,path,params=None,json_body=None):
           check(selected_profile,method,path+('?' + __import__('urllib.parse',fromlist=['urlencode']).urlencode(params,doseq=True) if params else ''),200,json_body)
           return {'ticket':{'tags':['existing']},'comments':[],'users':[],'groups':[]}
          namespace={'zd_request':dispatch,'load_agents_config':lambda:{'assignee_field_id':123}}
          exec(compile(ast.fix_missing_locations(selected),'actual-common-callers','exec'),namespace)
          for name,args,kw in [('fetch_ticket',(42,),{}),('fetch_ticket_comments',(42,),{}),('list_users',(),{'role':'agent'}),('list_groups',(),{}),('set_assignee_field',(42,456),{}),('set_ticket_assignee',(42,456),{'group_id':789}),('post_internal_note',(42,'synthetic'),{'tags':['new']})]:
           namespace[name](*args,**kw)
          selected_profile='public-reply'
          namespace['post_public_reply'](42,'synthetic',tags=['new'],remove_tags=['old'],status='pending')
          # Explicit 401 rejection is refreshed once; redirects are returned without following.
          statuses=iter([401,200])
          def unauthorized_then_ok(*args,**kw):
           r=response();r.status_code=next(statuses);return r
          upstream.side_effect=unauthorized_then_ok
          before=upstream.call_count
          check('read','GET','/api/v2/users/me.json',200)
          assert upstream.call_count==before+2;count+=1
          def redirect(*args,**kw):
           r=response();r.status_code=302;r.headers['Location']='https://other.invalid/';return r
          upstream.side_effect=redirect
          check('read','GET','/api/v2/users/me.json',302)
          assert upstream.call_args.kwargs['allow_redirects'] is False;count+=1
          upstream.side_effect=response
          bad=p/'bad.json';bad.write_text('{}')
          try:relay.build_app(dataclasses.replace(config,clients_file=str(bad)))
          except ValueError:count+=1
          else:raise AssertionError('empty policy accepted')
          legacy=relay.build_app(dataclasses.replace(config,clients_file='',db_file=p/'legacy.sqlite')).test_client()
          assert legacy.get('/api/v2/users/me.json',headers={'Authorization':'Bearer '+config.relay_token}).status_code==200;count+=1
         self.assertEqual(count, 141)


if __name__ == "__main__":
    unittest.main()
