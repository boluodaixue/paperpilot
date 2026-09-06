"""Complete rubric scoring with frozen settings and persisted attempt budgets."""
from __future__ import annotations
import argparse
import json
import re
import sys
import time

import freeze_protocol as freeze
from continuation_runtime import SPEC,ROOT,dump,verify_seal,delivery_issues
from formal_support import DIMS,validate_scores,summarize_scores

def judge(idx,arm='multi'):
    spec=verify_seal()
    if idx not in spec['debug_ids']+spec['holdout_ids'] or arm not in ('multi','single'):
        raise ValueError('Case outside frozen plan')
    folder=SPEC/'runs'/f'{idx}-{arm}'
    run=freeze.read(folder/'run.json')
    if run['status']!='returned' or run.get('delivery_issues'):
        raise ValueError('No scorable report')
    paper=(folder/'report.md').read_text(encoding='utf-8')
    if freeze.digest(paper.encode())!=run['report_sha256']:
        raise ValueError('Report changed')
    if delivery_issues(paper,run['output_status']):
        raise ValueError('No scorable report')
    selection=freeze.read(ROOT/'selection.json')
    row=next(r for r in selection['cases'] if r['idx']==idx)
    private_path=ROOT/'judge_inputs'/f'task-{idx}.json'
    if freeze.digest(private_path.read_bytes())!=row['judge_sha256']:
        raise ValueError('Rubric changed')
    private=freeze.read(private_path)
    template=(ROOT/'judge_prompt.txt').read_text(encoding='utf-8')
    from src.research.runtime import _sampling_kwargs
    from src.models.model_router import ModelRouter
    config=freeze.read(SPEC/'multi.config.json')
    backend=config['model']['backend_mapping']['judge']
    policy=ModelRouter.create_backend(backend,**_sampling_kwargs(config,'judge',backend))
    grading=spec['judge']
    protocol={'grading':grading,'report_sha256':run['report_sha256'],'rubric_sha256':row['judge_sha256']}
    lock=folder/'judge_protocol.json'
    if lock.exists() and freeze.read(lock)!=protocol:
        raise ValueError('Judge protocol changed')
    dump(lock,protocol)
    if (folder/'judge.json').exists():
        return freeze.read(folder/'judge.json')
    scores={d:[] for d in DIMS}
    run['judge_status']='running';dump(folder/'run.json',run)
    batches=folder/'judge_batches';batches.mkdir(exist_ok=True)
    try:
        for dim in DIMS:
            items=private['rubric'][dim]
            for offset in range(0,len(items),grading['chunk_size']):
                expected=items[offset:offset+grading['chunk_size']]
                saved=batches/f'{dim}-{offset}-accepted.json'
                if saved.exists():
                    accepted=validate_scores(freeze.read(saved),expected)
                else:
                    prompt=template.format(paper=paper,rubric=json.dumps({'task':private['task'],
                        'rubric_items':expected,'blocked':private.get('blocked',{})},ensure_ascii=False))
                    accepted=None
                    for attempt in range(grading['batch_attempts_max']):
                        record_path=batches/f'{dim}-{offset}-attempt-{attempt}.json'
                        if record_path.exists():
                            prior=freeze.read(record_path)
                            if prior.get('status')=='returned':
                                try:
                                    accepted=validate_scores(json.loads(prior['raw']),expected)
                                    break
                                except (ValueError,TypeError,KeyError):
                                    pass
                            continue
                        record={'status':'started','usage':None,'started_at':time.time()}
                        dump(record_path,record)
                        try:
                            response=policy.client.chat.completions.create(model=policy.model_name,
                                messages=[{'role':'user','content':prompt}],temperature=grading['temperature'],
                                top_p=grading['top_p'],max_tokens=grading['max_output_tokens_per_batch'])
                            raw=response.choices[0].message.content or ''
                            clean=re.sub(r'^```(?:json)?\s*|\s*```$','',raw.strip())
                            record.update(status='returned',raw=clean,finish_reason=response.choices[0].finish_reason,
                                          usage=response.usage.model_dump() if response.usage else None)
                            dump(record_path,record)
                            accepted=validate_scores(json.loads(clean),expected)
                        except Exception as exc:
                            record.update(status='failed',error_type=type(exc).__name__)
                        finally:
                            record['elapsed_seconds']=time.time()-record['started_at'];dump(record_path,record)
                        if accepted is not None:
                            break
                    if accepted is None:
                        raise ValueError('Batch attempts exhausted; no silent retry-budget reset')
                    dump(saved,{'results':accepted})
                scores[dim].extend(accepted)
                dump(folder/'judge_progress.json',{'completed':sum(map(len,scores.values())),'scores':scores})
        result={'protocol':protocol,'scores':scores,'summary':summarize_scores(scores)}
        dump(folder/'judge.json',result)
        run['judge_status']='valid';run.pop('judge_error_type',None);dump(folder/'run.json',run)
        print(json.dumps(result['summary']),flush=True)
        return result
    except Exception as exc:
        run.update(judge_status='failed',judge_error_type=type(exc).__name__);dump(folder/'run.json',run)
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--task',type=int,required=True);p.add_argument('--arm',choices=['multi','single'],required=True);args=p.parse_args()
    try:
        judge(args.task,args.arm)
    except Exception as exc:
        print(json.dumps({'judge_failed':type(exc).__name__}),flush=True);sys.exit(1)
