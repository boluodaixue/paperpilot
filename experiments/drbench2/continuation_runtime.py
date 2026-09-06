"""Frozen-default production Runtime adapter. Does not import pilot.py."""
from __future__ import annotations
import argparse
import asyncio
import copy
import json
import re
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlsplit

import freeze_protocol as freeze
from formal_support import SourceGuard,GuardedTool

ROOT=freeze.ROOT
SPEC=freeze.DEST

def dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_bytes(freeze.encode(value))
    tmp.replace(path)

def serializable(value):
    if hasattr(value,'value'):
        return value.value
    raise TypeError(type(value).__name__)

def verify_seal():
    manifest=freeze.verify()
    seal=freeze.read(SPEC/'adapter_seal.json')
    for name,expected in seal['files'].items():
        if freeze.digest((ROOT/name).read_bytes())!=expected:
            raise ValueError('Adapter changed after acceptance: '+name)
    if seal['offline_acceptance']!='passed':
        raise ValueError('Offline acceptance not passed')
    extension=freeze.read(SPEC/'continuation_seal.json')
    if extension['offline_acceptance']!='passed':
        raise ValueError('Continuation not accepted')
    for name,expected in extension['files'].items():
        if freeze.digest((ROOT/name).read_bytes())!=expected:
            raise ValueError('Continuation changed: '+name)
    return manifest

def case_input(idx):
    spec=freeze.read(ROOT/'selection.json')
    row=next(c for c in spec['cases'] if c['idx']==idx)
    p=ROOT/'research_inputs'/f'task-{idx}.json'
    if freeze.digest(p.read_bytes())!=row['research_sha256']:
        raise ValueError('Public task changed')
    data=freeze.read(p)
    if set(data)!={'idx','id','language','theme','description','prompt','blocked'}:
        raise ValueError('Unexpected research input field')
    return row,data

def run_config(arm,folder):
    c=freeze.read(SPEC/f'{arm}.config.json')
    c['research']['vault_root']=str(folder/'vault')
    c['research']['legacy_archive_root']=str(folder/'archive')
    c['runtime']['retrieval_db_path']=str(folder/'retrieval.db')
    c['chat']['db_path']=str(folder/'chat.db')
    changes=freeze.differences(freeze.read(SPEC/f'{arm}.config.json'),c)
    allowed=set(freeze.read(SPEC/'manifest.json')['runtime_path_overrides'])
    if set(changes)!=allowed:
        raise ValueError('Non-path runtime override')
    return c

class Ledger:
    def __init__(self,path):
        self.path=path;self.lock=threading.Lock();self.rows=[]
    def start(self,messages):
        with self.lock:
            row={'call':len(self.rows)+1,'status':'started','started_at':time.time(),
                 'input_characters':sum(len(str(m.get('content',''))) for m in messages),'usage':None}
            self.rows.append(row);dump(self.path,self.rows)
            return row
    def finish(self,row,response=None,error=None):
        with self.lock:
            row['elapsed_seconds']=time.time()-row['started_at']
            if error:
                row.update(status='raised',error_type=type(error).__name__)
            else:
                usage=response.get('usage')
                row.update(status='returned',finish_reason=response.get('finish_reason'),
                           usage=usage.model_dump() if hasattr(usage,'model_dump') else usage)
            dump(self.path,self.rows)

def metered_policy(config,ledger):
    from src.research.runtime import _build_policy
    base=_build_policy(config)
    cls=type(base)
    class Metered(cls):
        def __call__(self,messages,*,tools=None):
            row=ledger.start(messages)
            try:
                response=super().__call__(messages,tools=tools)
            except BaseException as exc:
                ledger.finish(row,error=exc);raise
            ledger.finish(row,response=response)
            return response
    base.__class__=Metered
    return base

def guarded_tools(config,guard):
    from src.research.runtime import build_research_tools
    tools=build_research_tools(config)
    result=[]
    for tool in tools:
        if tool.name=='acquire_evidence':
            tool.search_tool=GuardedTool(tool.search_tool,guard)
            tool.browser_tool=GuardedTool(tool.browser_tool,guard)
        if tool.name=='arxiv_reader':
            tool=GuardedTool(tool,guard)
        result.append(tool)
    if [t.name for t in result]!=config['tools']['enabled']:
        raise ValueError('Tool set changed')
    return result

def strip_frontmatter(text):
    # File metadata is an envelope, not report prose.
    return re.sub(r'\A---\r?\n.*?\r?\n---(?:\r?\n|$)','',text,count=1,flags=re.S).lstrip('\r\n')

def export_report(text,evidence,manifest,vault):
    from src.research.rendering import managed_note_id
    existing=set(manifest.evidence_paths)
    by_note={managed_note_id('Evidence',e.evidence_id):e.source_ref for e in evidence}
    issues=[];mapping=[]
    def replace(match):
        target,sep,label=match.group(1).partition('|')
        rel=target if target.endswith('.md') else target+'.md'
        source=by_note.get(Path(rel).stem)
        if rel not in existing or source is None:
            issues.append({'kind':'unresolved_wikilink','target':target});return match.group(0)
        physical=(vault/rel).resolve()
        if not physical.is_relative_to(vault.resolve()) or not physical.is_file():
            issues.append({'kind':'unpersisted_evidence','target':target});return match.group(0)
        url=urlsplit(source)
        if url.scheme not in ('http','https') or not url.netloc:
            issues.append({'kind':'non_web_source','target':target});return match.group(0)
        mapping.append({'original':match.group(0),'source_url':source})
        return '['+(label if sep else Path(target).stem)+']('+quote(source,safe=':/?=&%#@+;,~-._')+')'
    body=strip_frontmatter(text)
    exported=re.sub(r'\[\[([^\]]+)\]\]',replace,body)
    return exported,{'issues':issues,'replacements':mapping,'frontmatter_removed':text!=body}

def delivery_issues(report,status):
    body=strip_frontmatter(report).strip()
    if status not in ('valid','repaired'):
        return ['core_output_'+str(status)]
    if not body:
        return ['empty_report']
    if re.fullmatch(r'(?:```\w*\s*)?<(?P<tag>rdm_action|tool_call|tool_response)\b.*?</(?P=tag)>\s*(?:```)?',body,re.S|re.I):
        return ['tool_protocol_instead_of_report']
    if body.startswith('Error:') and len(body.splitlines())<=3:
        return ['error_instead_of_report']
    return []

async def invoke_production(config,folder,prompt,*,policy,tools,on_state=None):
    from src.research.runtime import open_research_runtime
    async with open_research_runtime(folder/'checkpoint.sqlite',config=config,policy=policy,tools=tools) as runtime:
        memory=await asyncio.to_thread(runtime.create_memory,'Benchmark research',memory_id='M-benchmark')
        result=await runtime.run_auto_confirmed(prompt,thread_id='benchmark-case',memory_id=memory.memory_id)
        if on_state:
            on_state(await runtime.get_state('benchmark-case'))
        return result

async def research(idx,arm):
    spec=verify_seal()
    row,public=case_input(idx)
    if idx not in spec['debug_ids']+spec['holdout_ids'] or arm not in ('multi','single'):
        raise ValueError('Case outside frozen plan')
    folder=SPEC/'runs'/f'{idx}-{arm}'
    folder.mkdir(parents=True,exist_ok=False)
    config=run_config(arm,folder)
    dump(folder/'effective_config.json',config)
    ledger=Ledger(folder/'model_calls.json')
    policy=metered_policy(config,ledger)
    guard=SourceGuard(public['blocked']);guard.install_http_guard()
    tools=guarded_tools(config,guard)
    state={'task_idx':idx,'arm':arm,'source_commit':spec['source_commit'],'status':'running',
           'protocol_id':spec['protocol_id'],'judge_status':'not_run','started_at':time.time(),
           'source_adapter_sha256':freeze.digest(Path(__file__).read_bytes()),
           'limits':config['research']['limits'],'tools':[t.name for t in tools]}
    dump(folder/'run.json',state)
    try:
        def save_state(s):
            dump(folder/'workflow_summary.json',{'confirmed':s.get('confirmed'),'workflow_status':s.get('workflow_status'),
                                               'deadline_at':s.get('deadline_at'),'memory_id':s.get('memory_id')})
        result=await invoke_production(config,folder,public['prompt'],policy=policy,tools=tools,on_state=save_state)
        raw=result.report_markdown
        (folder/'report.original.md').write_bytes(raw.encode('utf-8'))
        (folder/'workflow_result.json').write_text(json.dumps(asdict(result),ensure_ascii=False,indent=2,default=serializable),encoding='utf-8')
        exported,citations=export_report(raw,result.research_result.evidence,result.memory_manifest,folder/'vault')
        (folder/'report.md').write_bytes(exported.encode('utf-8'))
        dump(folder/'citation_export.json',citations)
        r=result.research_result
        problems=delivery_issues(exported,r.output_status.value)
        state.update(status='returned',research_status=r.status.value,output_status=r.output_status.value,
                     termination_reason=getattr(r.termination_reason,'value',None),stop_reason=r.stop_reason,
                     thread_count=r.thread_count,tool_calls=r.tool_calls_used,evidence_count=len(r.evidence),
                     estimated_tokens=r.estimated_tokens_used,delivery_issues=problems,
                     citation_export_issues=citations['issues'],report_sha256=freeze.digest(exported.encode('utf-8')),
                     report_characters=len(exported),judge_status='skipped_invalid_delivery' if problems else 'not_run')
    except BaseException as exc:
        state.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        state['elapsed_seconds']=time.time()-state['started_at']
        dump(folder/'run.json',state);dump(folder/'source_guard.json',guard.hits)
        print(json.dumps(state,ensure_ascii=True),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--task',type=int,required=True);p.add_argument('--arm',choices=['multi','single'],default='multi')
    args=p.parse_args()
    try:
        asyncio.run(research(args.task,args.arm))
    except BaseException as exc:
        print(json.dumps({'failed':type(exc).__name__}),flush=True);sys.exit(1)
