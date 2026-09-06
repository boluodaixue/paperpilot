"""Pure source guards and rubric validation; no pilot runtime or settings."""
import copy
import inspect
import json
import re
from urllib.parse import unquote,urljoin,urlsplit
DIMS=('info_recall','analysis','presentation')

def canonical(url: str) -> str:
    p = urlsplit(unquote(url).strip())
    return (p.netloc.lower().removeprefix('www.') + p.path.rstrip('/')).lower()

class SourceGuard:
    """Filter explicit forbidden URLs/title and known benchmark answer locations.

    This is an application guard, not a guarantee against every unknown mirror.
    """
    def __init__(self, blocked: dict):
        self.urls = [canonical(u) for u in blocked.get('urls', [])]
        self.paper_ids=[]
        for url in blocked.get('urls',[]):
            p=urlsplit(url)
            if 'arxiv.org' in p.netloc:
                self.paper_ids.extend(re.findall(r'\d{4}\.\d{4,5}(?:v\d+)?',p.path))
            if 'doi.org' in p.netloc:
                self.paper_ids.append(unquote(p.path).strip('/').casefold())
        self.title = re.sub(r'\W+', '', blocked.get('title', '').casefold())
        self.hits: list[dict] = []

    def matches(self, value: str) -> bool:
        s = unquote(str(value)).casefold()
        normalized = re.sub(r'\W+', '', s)
        return (any(u in s for u in self.urls)
                or any(paper_id in s for paper_id in self.paper_ids)
                or bool(self.title and len(self.title) > 12 and self.title in normalized)
                or any(x in s for x in ('tasks_and_rubrics', 'deepresearchbench2',
                                        'deepresearch-bench-ii', 'deepresearchbenchii')))

    def scrub(self, value):
        if isinstance(value, dict):
            identity = ' '.join(str(value.get(k, '')) for k in ('url', 'source_ref', 'title', 'final_url', 'link'))
            if self.matches(identity):
                self.hits.append({'kind': 'result_removed'})
                return None
            return {k: cleaned for k, v in value.items() if (cleaned := self.scrub(v)) is not None}
        if isinstance(value, list):
            return [cleaned for v in value if (cleaned := self.scrub(v)) is not None]
        if isinstance(value, str) and self.matches(value):
            self.hits.append({'kind': 'text_removed'})
            return '[Excluded benchmark reference]'
        return value

    def install_http_guard(self):
        import aiohttp
        original = aiohttp.ClientSession._request
        guard = self
        async def checked(session, method, url, **kwargs):
            follow = kwargs.pop('allow_redirects', True)
            current = str(url)
            for _ in range(6):
                if guard.matches(current):
                    guard.hits.append({'kind': 'http_blocked', 'url': current})
                    raise ValueError('Excluded benchmark reference')
                response = await original(session, method, current, allow_redirects=False, **kwargs)
                if not follow or response.status not in (301, 302, 303, 307, 308) or not response.headers.get('Location'):
                    return response
                destination = urljoin(str(response.url), response.headers['Location'])
                response.release()
                if response.status == 303 or (response.status in (301, 302) and method.upper() == 'POST'):
                    method = 'GET'
                    kwargs.pop('data', None)
                    kwargs.pop('json', None)
                current = destination
            raise ValueError('Too many redirects')
        aiohttp.ClientSession._request = checked

class GuardedTool:
    def __init__(self, inner, guard):
        self.inner, self.guard = inner, guard
    def __getattr__(self, name):
        return getattr(self.inner, name)
    def __deepcopy__(self, memo):
        return GuardedTool(copy.deepcopy(self.inner, memo), self.guard)
    async def execute(self, *args, **kwargs):
        if self.guard.matches(json.dumps({'args':args,'kwargs':kwargs}, ensure_ascii=False)):
            self.guard.hits.append({'kind': 'tool_blocked', 'tool': self.name})
            return {'error': 'Excluded benchmark reference; choose another source'}
        value = self.inner.execute(*args, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return self.guard.scrub(value) or {'error': 'Excluded benchmark reference'}

def validate_scores(payload, expected):
    if not isinstance(payload,dict) or set(payload) != {'results'} or not isinstance(payload['results'],list):
        raise ValueError('Invalid score schema')
    results=payload['results']
    if len(results)!=len(expected) or len({r.get('rubric_item') for r in results})!=len(expected):
        raise ValueError('Missing or duplicate rubric items')
    if {r.get('rubric_item') for r in results} != set(expected):
        raise ValueError('Rubric text mismatch')
    for r in results:
        if type(r.get('score')) is not int or r['score'] not in (-1,0,1):
            raise ValueError('Invalid score')
        if not isinstance(r.get('reason'),str) or not r['reason'].strip() or not isinstance(r.get('evidence'),str):
            raise ValueError('Missing reason/evidence')
        if r['score'] != 0 and not r['evidence'].strip():
            raise ValueError('Positive/blocked score requires evidence')
    by_text={r['rubric_item']:r for r in results}
    return [by_text[item] for item in expected]

def summarize_scores(scores):
    out={}
    all_scores=[]
    for dim in DIMS:
        vals=[x['score'] for x in scores[dim]]
        out[dim]={'count':len(vals),'pass_rate':sum(x==1 for x in vals)/len(vals) if vals else None}
        all_scores.extend(vals)
    out['total_pass_rate']=sum(x==1 for x in all_scores)/len(all_scores) if all_scores else None
    out['blocked_rate']=sum(x==-1 for x in all_scores)/len(all_scores) if all_scores else None
    return out
