"""Metadata-only recommendation with authoritative final verification."""
import json
import uuid
import time

try:
    from server.search import normalize, query_concepts
except ModuleNotFoundError:
    from search import normalize, query_concepts
from . import prompts
from .documents import compact
from .provider import ProviderError
from .schema import json_object, validate_plan, validate_request

TITLE_WEIGHT = 3.0
PREFERRED_WEIGHT = 2.0
PAGE_WEIGHT = 0.5
RRF_K = 60


def matches(record, required, excluded, plan):
    pairs = {(t['type'], t['id']) for t in record['tags']}
    if any((t['type'], t['id']) not in pairs for t in required) or any((t['type'], t['id']) in pairs for t in excluded):
        return False
    if record['id'] in plan['excluded_gallery_ids']:
        return False
    pages, bounds = record.get('num_pages'), plan['page_range']
    if bounds['hard']:
        for key in ('min', 'max'):
            if bounds[key] is not None and (pages is None or (pages < bounds[key] if key == 'min' else pages > bounds[key])):
                return False
    return True


class Recommender:
    def __init__(self, library, provider, config, vectors):
        self.library, self.provider, self.config, self.vectors = library, provider, config, vectors

    def _chat(self, system, data, purpose, validator, warnings):
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]
        for attempt in range(2):
            try:
                response = self.provider.chat(model=self.config['parser_model' if purpose == 'parse' else 'quality_model'],
                    messages=messages, max_tokens=600 if purpose == 'parse' else 250, temperature=.1, purpose=purpose)
            except ProviderError:
                warnings.append(f'{purpose}: remote unavailable; local metadata fallback.')
                return None
            try:
                return validator(json_object(response.text))
            except (ValueError, TypeError, KeyError):
                messages.extend([{'role': 'assistant', 'content': response.text[:3000]},
                                 {'role': 'user', 'content': prompts.REPAIR + ' Validation failed: invalid schema or candidate IDs.'}])
        warnings.append(f'{purpose}: invalid model response; local metadata fallback.')
        return None

    def recommend(self, request, progress=None):
        started = time.monotonic()
        timings = {}
        last = [started, 'parse']
        def stage(name):
            now = time.monotonic()
            timings[last[1]] = round(now-last[0], 3)
            last[:] = [now, name]
            if progress:
                progress(name)
        if progress:
            progress('parse')
        request = validate_request(request, self.config['result_limit'])
        warnings = []
        count = request['limit']
        plan = self._chat(prompts.PARSER, request, 'parse', lambda v: validate_plan(v, count), warnings) if self.provider.configured else None
        if plan is None:
            # Preserve already validated hard conditions during an offline follow-up.
            plan = validate_plan((request['previous_plan'] or {}) | {'semantic_query': request['message'], 'requested_count': count}, count)
            warnings.append('Natural-language interpretation unavailable; using local search and previous filters.')
        stage('filters')
        count = min(count, plan['requested_count'])
        if request['mode'] == 'deep' or plan['visual_query'] or plan['narrative_query']:
            warnings.append('V1 uses metadata only; visual and narrative analysis is unavailable.')
        plan['mode'] = 'fast'
        unresolved, required, excluded = [], [], []
        for key, target in [('required', required), ('excluded', excluded)]:
            for term in plan[key]:
                resolved = self.library.assistant_resolve(term)
                if resolved:
                    target.append(resolved)
                else:
                    unresolved.append(dict(term) | {'constraint': key})
        response = dict(timings=timings, request_id=uuid.uuid4().hex, status='ready', assistant_text='Recommendations based on local metadata.',
                        plan=plan, unresolved_terms=unresolved, results=[], warnings=warnings)
        if unresolved:
            warnings.append('Some taxonomy terms are unresolved or ambiguous and were not applied as hard filters.')
        bounds = plan['page_range']
        allowed, after = set(), 0
        while True:
            batch = self.library.assistant_filter_candidates(required_terms=required, excluded_terms=excluded,
                min_pages=bounds['min'] if bounds['hard'] else None, max_pages=bounds['max'] if bounds['hard'] else None, after_id=after)
            if not batch:
                break
            allowed.update(batch)
            after = batch[-1]
        allowed.difference_update(int(i) for i in plan['excluded_gallery_ids'])
        if not allowed:
            response['assistant_text'] = 'No downloaded galleries match these filters.'
            stage('ready')
            response['elapsed_seconds'] = round(time.monotonic()-started, 3)
            return response
        stage('semantic')
        dense = {}
        self.vectors.refresh()
        if self.provider.configured and len(self.vectors.snapshot[1]):
            try:
                vector = self.provider.embed_texts(model=self.config['embedding_model'], texts=[plan['semantic_query'] or request['message']], input_type='query', purpose='query')[0]
                dense = dict(self.vectors.search(vector, allowed=allowed, limit=self.config['dense_candidate_count']))
            except (ProviderError, ValueError, IndexError):
                warnings.append('Semantic search unavailable; using local metadata ordering.')
        stage('local_search')
        lexical, _ = self.library.search(plan['semantic_query'] or request['message'], per_page=self.config['dense_candidate_count'])
        lexical_ids = {int(r['id']) for r in lexical} & allowed
        concepts = query_concepts(plan['semantic_query'] or request['message'], self.library.search_aliases)
        preferred = [(term, self.library.assistant_resolve(term)) for term in plan['preferred'] if term['kind'] in {'tag', 'artist', 'character', 'parody', 'group', 'language', 'category'}]
        scored, records = [], {}
        pool = set(dense) | lexical_ids
        budget = min(self.config['dense_candidate_count'], 100)
        # SQL identifies exact preferences without loading every gallery/taxonomy
        # or stat-ing 15k archives. Only this bounded pool is materialized below.
        for _, term in preferred:
            if term:
                pool.update(set(self.library.assistant_filter_candidates(
                    required_terms=required+[term], excluded_terms=excluded,
                    min_pages=bounds['min'] if bounds['hard'] else None,
                    max_pages=bounds['max'] if bounds['hard'] else None, limit=budget)) & allowed)
            if len(pool) >= 400:
                break
        if required or bounds['min'] or bounds['max']:
            pool.update(set(self.library.assistant_filter_candidates(
                required_terms=required, excluded_terms=excluded,
                min_pages=bounds['min'] if bounds['hard'] else None,
                max_pages=bounds['max'] if bounds['hard'] else None,
                prefer_short=bool(bounds['max']), prefer_long=bool(bounds['min'] and not bounds['max']),
                limit=budget)) & allowed)
        ranked_pool = list(dict.fromkeys([*dense, *sorted(lexical_ids), *sorted(pool)]))
        ids = [gid for gid in ranked_pool if gid in allowed][:400]
        for offset in range(0, len(ids), 500):
            for record in self.library.assistant_records(ids[offset:offset+500]):
                if not matches(record, required, excluded, plan):
                    continue
                gid = int(record['id'])
                title = normalize(record['title'])
                names = ' '.join(normalize(t['name']) for t in record['tags'])
                score = sum(TITLE_WEIGHT if any(t in title for t in alternatives) else 1.0 if any(t in names for t in alternatives) else 0 for alternatives, _ in concepts)
                pairs = {(t['type'], t['id']) for t in record['tags']}
                for term, resolved in preferred:
                    if resolved and (resolved['type'], resolved['id']) in pairs:
                        score += PREFERRED_WEIGHT * term['weight']
                pages = record.get('num_pages')
                if pages and not bounds['hard']:
                    if bounds['max']:
                        score += PAGE_WEIGHT * min(1, bounds['max'] / pages)
                    if bounds['min']:
                        score += PAGE_WEIGHT * min(1, pages / bounds['min'])
                if gid in lexical_ids:
                    score += TITLE_WEIGHT
                if score > 0 or gid in dense or required or bounds['hard']:
                    records[gid] = record
                    scored.append((gid, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        # RRF balances independent rankings without comparing cosine to text scores.
        metadata_rank = {gid: rank for rank, (gid, score) in enumerate(scored, 1) if score > 0 or required or bounds['hard']}
        dense_rank = {gid: rank for rank, gid in enumerate(dense, 1) if gid in records}
        fused = sorted(records, key=lambda gid: (-((1/(RRF_K+metadata_rank[gid]) if gid in metadata_rank else 0) + (1/(RRF_K+dense_rank[gid]) if gid in dense_rank else 0)), gid))
        candidates = fused[:self.config['rerank_candidate_count']]
        selected = candidates[:count]
        packet = [compact(records[gid]) | {'local_scores': {'dense': dense.get(gid, 0), 'metadata': dict(scored)[gid]}} for gid in candidates]
        def validate_ranking(value):
            if set(value) - {'results', 'assistant_text'} or not isinstance(value.get('results'), list) or len(value['results']) > 100:
                raise ValueError('invalid ranking')
            chosen = []
            for item in value['results']:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    raise ValueError('invalid result')
                if item['id'].isdigit() and int(item['id']) in candidates and int(item['id']) not in chosen:
                    chosen.append(int(item['id']))
            if value['results'] and not chosen:
                raise ValueError('unknown candidate IDs')
            return chosen[:count]
        stage('rerank')
        if packet and self.provider.configured and self.config['rerank_enabled']:
            ranking = self._chat(prompts.RERANK, dict(candidates=packet, requested_count=count, plan=plan), 'rerank', validate_ranking, warnings)
            if ranking is not None:
                selected = ranking
        stage('verify')
        for gid in selected:
            record = self.library.assistant_gallery(str(gid))
            if not record or not matches(record, required, excluded, plan):
                continue
            # Render only verifiable metadata reasons; free-form model claims are advisory.
            evidence = [{'type': 'metadata', 'label': f"{t['type']}: {t['name']}"} for t in record['tags'] if any(t['type'] == x['type'] and t['id'] == x['id'] for x in required + [r for _, r in preferred if r])][:3]
            if not evidence:
                evidence = [{'type': 'metadata', 'label': f"{t['type']}: {t['name']}"} for t in record['tags'][:3]]
            reasons = [e['label'] for e in evidence] or ['Matched local title metadata.']
            if record.get('num_pages'):
                reasons = [f"{record['num_pages']} pages"] + reasons[:2]
            response['results'].append(dict(id=str(gid), title=record['title'], pages=record.get('num_pages'),
                detail_url=f'/g/{gid}/', cover_url=f'/catalog-thumbnail/{gid}', reasons=reasons, evidence=evidence,
                match_sources=['metadata'] + (['semantic'] if gid in dense else [])))
        if not response['results']:
            response['assistant_text'] = 'No downloaded galleries match this request.'
        stage('ready')
        response['elapsed_seconds'] = round(time.monotonic()-started, 3)
        return response
