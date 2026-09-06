"""Offline experiment specification and integrity checks. No online run command.

This does not import pilot.py or its reduced budgets/model overrides.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import socket
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
SNAPSHOT=ROOT/'paperpilot_snapshot'
DEST=ROOT/'formal-v1'
SOURCE_COMMIT='9e81efed413b86152cfcff372e379b350bfa9e38'

def digest(data):
    return hashlib.sha256(data).hexdigest()

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def encode(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')

def build_arm(default,arm):
    c=copy.deepcopy(default)
    if arm=='single':
        c['research']['limits'].update(max_fork_depth=0,max_children_per_agent=0,
                                       max_children=0,max_total_agents=1,
                                       max_total_threads=1,max_concurrent_agents=1)
    elif arm!='multi':
        raise ValueError('Unknown arm')
    return c

def differences(a,b,prefix=''):
    result={}
    for k in sorted(a.keys()|b.keys()):
        p=f'{prefix}.{k}' if prefix else k
        x,y=a.get(k),b.get(k)
        if isinstance(x,dict) and isinstance(y,dict):
            result.update(differences(x,y,p))
        elif x!=y:
            result[p]={'default':x,'arm':y}
    return result

def deny_network(*args,**kwargs):
    raise RuntimeError('Phase 1 is offline; network access denied')

def observations():
    # Socket denial covers constructor side effects as well as accidental API use.
    with patch.object(socket.socket,'connect',deny_network), patch.object(socket.socket,'connect_ex',deny_network), patch('socket.create_connection',deny_network):
        sys.path.insert(0,str(SNAPSHOT))
        from src.research.runtime import load_config,_build_policy,_sampling_kwargs,build_research_tools,limits_from_config
        from src.models.model_router import ModelRouter
        from src.utils.env_config import get_env
        config=load_config(SNAPSHOT/'configs/default.yaml')
        policy=_build_policy(config)
        judge_backend=config['model']['backend_mapping']['judge']
        judge=ModelRouter.create_backend(judge_backend,**_sampling_kwargs(config,'judge',judge_backend))
        tools=build_research_tools(config)
        acquire=next(t for t in tools if t.name=='acquire_evidence')
        paper=next(t for t in tools if t.name=='arxiv_reader')
        def model_info(p):
            return {'model':p.model_name,'temperature':p.temperature,'top_p':p.top_p,
                    'default_max_tokens':p.max_tokens,'max_input_chars':p.max_input_chars,
                    'request_timeout':p.client.timeout.as_dict(),'sdk_max_retries':p.client.max_retries,
                    'endpoint_sha256':digest(str(p.client.base_url).encode()),
                    'credential_present':bool(p.client.api_key),
                    'client_class':type(p.client).__module__+'.'+type(p.client).__name__}
        observed={'research_model':model_info(policy),'judge_model':model_info(judge),
                  'tools':[t.name for t in tools],
                  'search_backend':acquire.search_tool.backend,
                  'search_fallback_setting':get_env('SEARCH_FALLBACK_BACKENDS','<project default>'),
                  'paper_backend':paper.backend,'browser_timeout':acquire.browser_tool.timeout,
                  'provider_key_presence':{k:bool(os.environ.get(k)) for k in (
                      'TAVILY_API_KEY','METASO_API_KEY','EXA_API_KEY','BOCHA_API_KEY','SERPAPI_API_KEY',
                      'SEMANTIC_SCHOLAR_API_KEY','OPENALEX_API_KEY')}}
        for arm in ('single','multi'):
            limits_from_config(build_arm(config,arm)).validate()
    return config,observed

def source_files():
    z=zipfile.ZipFile(ROOT/'paperpilot-9e81efe.zip')
    hashes={}
    for info in z.infolist():
        if info.is_dir():
            continue
        data=z.read(info)
        actual=(SNAPSHOT/info.filename).read_bytes()
        if actual!=data:
            raise ValueError('Research source differs from frozen git archive: '+info.filename)
        hashes[info.filename]=digest(actual)
    return hashes

def collect():
    config,observed=observations()
    selection=read(ROOT/'selection.json')
    protected={}
    for path in [ROOT/'selection.json',ROOT/'upstream_manifest.json',ROOT/'judge_prompt.txt']:
        protected[str(path.relative_to(ROOT))]=digest(path.read_bytes())
    for case in selection['cases']:
        for kind in ('research','judge'):
            p=ROOT/f'{kind}_inputs'/f"task-{case['idx']}.json"
            h=digest(p.read_bytes())
            if h!=case[kind+'_sha256']:
                raise ValueError('Previously frozen benchmark input changed')
            protected[str(p.relative_to(ROOT))]=h
    if digest((ROOT/'judge_prompt.txt').read_bytes())!=selection['judge_prompt_sha256']:
        raise ValueError('Official judge prompt changed')
    versions={}
    for pkg in ('openai','langgraph','langgraph-checkpoint-sqlite','aiohttp','httpx','httpx2','PyYAML','langfuse'):
        try:
            versions[pkg]=importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg]=None
    manifest={'protocol_id':'paperpilot-drbench2-formal-v1','phase':'1_specification_frozen',
        'online_calls_started':False,'source_commit':SOURCE_COMMIT,
        'source_archive_sha256':digest((ROOT/'paperpilot-9e81efe.zip').read_bytes()),
        'source_files':source_files(),'protected_inputs':protected,'observed_environment':observed,
        'python':platform.python_version(),'dependencies':versions,
        'entry':{'runtime_factory':'src.research.runtime.open_research_runtime',
                 'call':'ResearchRuntime.run_auto_confirmed',
                 'flow':['start','Brief interrupt','review(confirm)','planning','AgentGraph','persist_result'],
                 'excluded_previous_entry':'src.research.core.run_core_research',
                 'fresh_managed_memory_per_attempt':True,
                 'fixed_plan_injection':False},
        'debug_ids':[c['idx'] for c in selection['cases'] if c['split']=='debug'],
        'holdout_ids':[c['idx'] for c in selection['cases'] if c['split']=='holdout'],
        'single_changes':differences(config,build_arm(config,'single')),
        'judge':{'protocol':'upstream rubric and prompt; project DeepSeek Judge, non-official',
                 'model':observed['judge_model']['model'],'temperature':0.1,'top_p':observed['judge_model']['top_p'],
                 'max_output_tokens_per_batch':8192,'chunk_size':12,'report_truncation':False,
                 'batch_attempts_max':2,'sdk_max_retries':observed['judge_model']['sdk_max_retries'],
                 'timeout':observed['judge_model']['request_timeout'],
                 'aggregation':'fraction(score==1); score 0 and -1 remain in denominator',
                 'missing_score':None,'offline_dimension_weights':'none; total weighted by item counts'},
        'runtime_path_overrides':['research.vault_root','research.legacy_archive_root','runtime.retrieval_db_path','chat.db_path'],
        'allowed_benchmark_adaptations':['blocked reference guard on all retrieval paths',
            'fresh run-local managed Memory and databases','deterministic citation export to source URLs',
            'passive metering without truncation, retries or sampling overrides'],
        'research_case_attempts':1,'parallel_cases':1,'cross_arm_plan_policy':'independent production Brief and planner calls',
        'previous_runs_excluded':['debug-v1','debug-v2','debug-v3'],
        'phase2_preconditions':['new production-path adapter offline checks pass',
            'adapter source hash recorded before any call','all frozen hashes and environment verified'],
        'adapter_status':'previous pilot runner rejected; production-path adapter implementation remains a launch prerequisite'}
    return config,manifest

def freeze():
    config,m=collect()
    DEST.mkdir(exist_ok=False)
    (DEST/'multi.config.json').write_bytes(encode(build_arm(config,'multi')))
    (DEST/'single.config.json').write_bytes(encode(build_arm(config,'single')))
    (DEST/'manifest.json').write_bytes(encode(m))
    return m

def verify():
    lock=read(DEST/'freeze_lock.json')
    for relative,expected in lock['files'].items():
        if digest((ROOT/relative).read_bytes())!=expected:
            raise ValueError('Sealed specification or verifier changed: '+relative)
    config,current=collect()
    saved=read(DEST/'manifest.json')
    if saved!=current:
        raise ValueError('Frozen source, inputs, settings or runtime environment changed')
    for arm in ('single','multi'):
        if read(DEST/f'{arm}.config.json')!=build_arm(config,arm):
            raise ValueError('Frozen arm configuration changed')
    return saved

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=('freeze','verify'))
    args=p.parse_args();m=freeze() if args.command=='freeze' else verify()
    print(json.dumps({'result':'passed','network':'disabled','phase':m['phase'],
                      'research_model':m['observed_environment']['research_model'],
                      'tools':m['observed_environment']['tools'],
                      'single_changed_fields':list(m['single_changes'])},ensure_ascii=True))
