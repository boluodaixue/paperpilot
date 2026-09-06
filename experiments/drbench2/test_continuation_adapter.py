import asyncio
import json
import re
import socket
import unittest
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import freeze_protocol as f
import continuation_runtime as a
from formal_support import SourceGuard,GuardedTool,validate_scores

class FixturePolicy:
    def __init__(self): self.stages=[]
    def __call__(self,messages,*,tools=None):
        first=str(messages[0].get('content',''));last=str(messages[-1].get('content',''))
        def response(obj): return {'content':json.dumps(obj),'tool_calls':[]}
        if 'before research begins' in first:
            self.stages.append('brief')
            return response({'objective':'Verify fixture evidence','scope':['fixture evidence'],
                             'directions':['Verify fixture evidence'],'constraints':[], 'expected_output':'Markdown report'})
        if 'PaperPilot Research Planner' in first:
            self.stages.append('planner')
            return response({'core_questions':['Verify fixture evidence'],'report_outline':['Findings'],
                             'source_guidance':[],'work_hints':[]})
        if first.startswith('Choose the next control action'):
            s=json.loads(last)
            return response({'action':'local_research','rationale':'One fixture scope',
                             'target_requirement_ids':[r['requirement_id'] for r in s['requirements']], 'fork_candidates':[]})
        if last.startswith('ASSESS_RESEARCH_STATE'):
            self.stages.append('assessment')
            state=json.loads(last.split('STATE:\n',1)[1])
            coverage=[{'requirement_id':r['requirement_id'],'status':'supported',
                'evidence_ids':[e['evidence_id'] for e in state['evidence']],
                'rationale':'Fixture is supported','remaining_gap':None} for r in state['requirements']]
            return response({'decision':'stop_research','coverage':coverage,'critical_gaps':[],
                             'next_actions':[],'termination_reason':'coverage_complete','replan_reason':None,'exhaustion_reason':None})
        if tools==[]:
            self.stages.append('report')
            ids=re.findall(r'evidence-[0-9a-f]{16}\b',json.dumps(messages))
            suffix=' [[EVIDENCE:'+ids[0]+']]' if ids else ''
            return {'content':'# Fixture report\n\nFixture evidence supports this finding.'+suffix,'tool_calls':[]}
        self.stages.append('tool')
        return {'content':'','tool_calls':[{'id':'fixture-call','type':'function',
            'function':{'name':'acquire_evidence','arguments':json.dumps({'query':'fixture evidence'})}}]}

class FixtureAcquire:
    name='acquire_evidence'
    async def execute(self,**kwargs):
        return {'query':'fixture evidence','search_backend':'offline','documents':[{'url':'https://example.org/fixture',
            'title':'Fixture evidence','format':'html','blocks':[{'locator':'paragraph 1',
            'heading':'Fixture evidence','text':'Fixture evidence supports this finding.'}]}],
            'candidates':[],'metrics':{'opened_count':1,'candidate_count':1}}
    def get_openai_tool_schema(self):
        return {'type':'function','function':{'name':self.name,'description':'Offline fixture',
                                            'parameters':{'type':'object','properties':{'query':{'type':'string'}}}}}

class FormalAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        f.verify()

    def folder(self):
        p=f.ROOT/'formal_offline_tests'/uuid.uuid4().hex;p.mkdir(parents=True);return p

    def test_full_runtime_brief_confirm_plan_persist_without_network(self):
        folder=self.folder();config=a.run_config('multi',folder);policy=FixturePolicy()
        from src.research.runtime import build_research_tools
        async def exercise():
            from langsmith import tracing_context
            with tracing_context(enabled=False),patch.object(socket.socket,'connect',f.deny_network),patch.object(socket.socket,'connect_ex',f.deny_network):
                return await a.invoke_production(config,folder,'Verify fixture evidence',policy=policy,tools=tools,on_state=observed.append)
        with patch.object(socket.socket,'connect',f.deny_network),patch.object(socket.socket,'connect_ex',f.deny_network):
            tools=[FixtureAcquire() if t.name=='acquire_evidence' else t for t in build_research_tools(config)]
            observed=[]
        result=asyncio.run(exercise())
        self.assertTrue(observed[0]['confirmed'])
        self.assertEqual(policy.stages[0],'brief')
        self.assertIn('planner',policy.stages)
        self.assertIn('tool',policy.stages)
        self.assertTrue((folder/'vault'/result.memory_manifest.report_path).is_file())
        json.dumps(asdict(result),default=a.serializable)
        self.assertGreater(len(result.research_result.evidence),0)
        exported,audit=a.export_report(result.report_markdown,result.research_result.evidence,result.memory_manifest,folder/'vault')
        self.assertFalse(audit['issues'])
        self.assertIn('https://example.org/fixture',exported)
        self.assertNotIn('[[Memories/',exported)

    def test_only_storage_paths_are_overridden(self):
        config=a.run_config('multi',self.folder())
        differences=f.differences(f.read(f.DEST/'multi.config.json'),config)
        self.assertEqual(set(differences),set(f.read(f.DEST/'manifest.json')['runtime_path_overrides']))

    def test_meter_does_not_change_policy_or_client_settings(self):
        from src.research.runtime import _build_policy
        config=f.read(f.DEST/'multi.config.json')
        original=_build_policy(config);metered=a.metered_policy(config,a.Ledger(self.folder()/'calls.json'))
        for name in ('model_name','temperature','top_p','max_tokens','max_input_chars'):
            self.assertEqual(getattr(original,name),getattr(metered,name))
        self.assertEqual(original.client.timeout,metered.client.timeout)
        self.assertEqual(original.client.max_retries,metered.client.max_retries)

    def test_all_default_tools_present_and_paper_reader_guarded(self):
        config=f.read(f.DEST/'multi.config.json');tools=a.guarded_tools(config,SourceGuard({}))
        self.assertEqual([t.name for t in tools],config['tools']['enabled'])
        self.assertIsInstance(next(t for t in tools if t.name=='arxiv_reader'),GuardedTool)

    def test_short_real_text_not_rejected_by_arbitrary_length(self):
        self.assertEqual(a.delivery_issues('# Result\n\nA finding with a source.','valid'),[])
        self.assertTrue(a.delivery_issues('<rdm_action>file_reader</rdm_action>','valid'))

    def test_public_case_has_no_rubric(self):
        _,case=a.case_input(25)
        self.assertNotIn('content',case);self.assertNotIn('rubric',case)

    def test_unknown_reference_preserved_not_fabricated(self):
        text,audit=a.export_report('A [[Memories/M-x/evidence/Unknown]]',[],SimpleNamespace(evidence_paths=[]),self.folder())
        self.assertTrue(audit['issues']);self.assertNotIn('https://',text)

    def test_judge_missing_items_not_success(self):
        with self.assertRaises(ValueError): validate_scores({'results':[]},['required item'])

    def test_paper_identifier_cannot_bypass_guard(self):
        g=SourceGuard({'urls':['https://arxiv.org/abs/2501.12345']})
        self.assertTrue(g.matches('2501.12345v2'))

    def scoring_fixture(self):
        import continuation_score as score
        spec=f.read(f.DEST/'manifest.json');root=self.folder();folder=root/'runs/25-multi';folder.mkdir(parents=True)
        (root/'multi.config.json').write_bytes((f.DEST/'multi.config.json').read_bytes())
        body='# Offline score fixture\n\nThis is only an offline test.'
        (folder/'report.md').write_bytes(body.encode())
        a.dump(folder/'run.json',{'status':'returned','output_status':'valid','delivery_issues':[],
                                 'report_sha256':f.digest(body.encode()),'judge_status':'not_run'})
        return score,spec,root,folder

    def test_full_rubric_scoring_and_no_duplicate_completed_calls(self):
        score,spec,root,folder=self.scoring_fixture();calls=[]
        def create(**kwargs):
            calls.append(kwargs)
            prompt=kwargs['messages'][0]['content']
            payload=json.loads(prompt.split('<task_and_rubric>\n',1)[1].split('\n</task_and_rubric>',1)[0])
            raw=json.dumps({'results':[{'rubric_item':x,'score':0,'reason':'Offline fixture only','evidence':''} for x in payload['rubric_items']]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw),finish_reason='stop')],
                                   usage=SimpleNamespace(model_dump=lambda:{'total_tokens':1}))
        policy=SimpleNamespace(model_name=spec['judge']['model'],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        from src.models.model_router import ModelRouter
        with patch.object(score,'SPEC',root),patch.object(score,'verify_seal',return_value=spec),patch.object(ModelRouter,'create_backend',return_value=policy),patch.object(socket.socket,'connect',f.deny_network):
            result=score.judge(25);before=len(calls);score.judge(25)
        self.assertEqual(sum(map(len,result['scores'].values())),102)
        self.assertEqual(len(calls),before)
        self.assertTrue(all(c['max_tokens']==8192 and c['temperature']==.1 for c in calls))

    def test_scoring_restart_does_not_reset_failed_attempt_budget(self):
        score,spec,root,folder=self.scoring_fixture();calls=[]
        def create(**kwargs):
            calls.append(1);raise TimeoutError('Offline forced timeout')
        policy=SimpleNamespace(model_name=spec['judge']['model'],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        from src.models.model_router import ModelRouter
        with patch.object(score,'SPEC',root),patch.object(score,'verify_seal',return_value=spec),patch.object(ModelRouter,'create_backend',return_value=policy),patch.object(socket.socket,'connect',f.deny_network):
            with self.assertRaises(ValueError): score.judge(25)
            self.assertEqual(len(calls),2)
            with self.assertRaises(ValueError): score.judge(25)
        self.assertEqual(len(calls),2)

if __name__=='__main__': unittest.main()
