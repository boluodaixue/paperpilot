"""Freeze a metadata-only stratified pilot; never select by observed scores."""
from __future__ import annotations
import ast
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEED = 'paperpilot-drbench2-pilot-20260906-v1'
DIMS = ('info_recall', 'analysis', 'presentation')

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def prepare() -> dict:
    provenance = json.loads((ROOT / 'upstream_manifest.json').read_text(encoding='utf-8'))
    upstream = ROOT / 'upstream' / provenance['commit']
    for name, expected in provenance['files'].items():
        assert digest((upstream / name).read_bytes()) == expected, name
    rows = [json.loads(line) for line in (upstream / 'tasks_and_rubrics.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    assert len({r['idx'] for r in rows}) == len(rows)
    for row in rows:
        c = row['content']
        assert all(isinstance(c['rubric'][d], list) and c['rubric'][d] for d in DIMS)
        assert isinstance(c.get('blocked', {}), dict)
        assert isinstance(row['prompt'], str) and row['prompt'].strip()
    counts = Counter(r['theme'] for r in rows)
    themes = sorted(counts, key=lambda t: (-counts[t], t))[:6]
    selected = []
    for i, theme in enumerate(themes):
        for lang in ('zh', 'en'):
            options = [r for r in rows if r['theme'] == theme and r['language'] == lang]
            row = min(options, key=lambda r: digest(f"{SEED}:{r['idx']}".encode()))
            # Two different domains and languages reserved for debugging.
            split = 'debug' if (i == 0 and lang == 'zh') or (i == 1 and lang == 'en') else 'holdout'
            public = {k: row[k] for k in ('idx', 'id', 'language', 'theme', 'description', 'prompt')}
            public['blocked'] = row['content'].get('blocked', {})
            public_path = ROOT / 'research_inputs' / f"task-{row['idx']}.json"
            judge_path = ROOT / 'judge_inputs' / f"task-{row['idx']}.json"
            write_json(public_path, public)
            write_json(judge_path, row['content'])
            selected.append({k: public[k] for k in ('idx', 'id', 'language', 'theme', 'description')} | {
                'split': split, 'research_sha256': digest(public_path.read_bytes()),
                'judge_sha256': digest(judge_path.read_bytes()),
                'rubric_counts': {d: len(row['content']['rubric'][d]) for d in DIMS}})
    tree = ast.parse((upstream / 'run_evaluation.py').read_text(encoding='utf-8-sig'))
    template = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PROMPT_TEMPLATE' for t in n.targets))
    template_path = ROOT / 'judge_prompt.txt'
    template_path.write_text(template, encoding='utf-8')
    manifest = {'seed': SEED, 'upstream': provenance, 'dataset_size': len(rows),
                'themes': themes, 'selection_rule': 'six largest themes (alphabetical tie break), minimum SHA256(seed:idx) per theme/language; first theme zh and second theme en reserved for debug',
                'judge_prompt_sha256': digest(template_path.read_bytes()), 'cases': selected}
    target = ROOT / 'selection.json'
    if target.exists():
        assert json.loads(target.read_text(encoding='utf-8')) == manifest, 'Frozen selection changed'
    write_json(target, manifest)
    lines = ['# 固定题目清单', '', f"上游提交：`{provenance['commit']}`。原始题库 {len(rows)} 题。", '',
             '按题库元数据选择样本，未参考模型输出或评分。调试 2 题，保留试评 10 题；6 个领域、中英文各 6 题。', '',
             '| idx | 用途 | 语言 | 领域 | 任务 |', '|---|---|---|---|---|']
    for c in selected:
        lines.append(f"| {c['idx']} | {c['split']} | {c['language']} | {c['theme']} | {c['description'].replace('|', '/')} |")
    lines += ['', '评分材料只存在 judge_inputs；研究工具只允许访问各次运行的 Artifact 目录。提示中保留官方禁止参考资料约束。',
              '这是开发用固定子集，不是官方全量评测；不得将其分数直接与榜单比较。']
    (ROOT / '题目清单.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return manifest

if __name__ == '__main__':
    m = prepare()
    print(json.dumps({'tasks': len(m['cases']), 'debug': [c['idx'] for c in m['cases'] if c['split']=='debug'], 'holdout': [c['idx'] for c in m['cases'] if c['split']=='holdout']}))
