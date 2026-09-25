"""Strict, bounded JSON contracts shared by requests and model output."""
import json
import math
import re

KINDS = {'tag', 'artist', 'character', 'parody', 'group', 'language', 'category'}
SOFT_KINDS = {'theme', 'mood', 'visual_style', 'scene', 'narrative'}
KEYS = {'required', 'preferred', 'excluded', 'page_range', 'semantic_query', 'visual_query', 'narrative_query', 'mode', 'requested_count', 'excluded_gallery_ids'}


def json_object(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object')
    return value


def bounded_int(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('integer out of range')
    return value


def gallery_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[1-9][0-9]{0,17}', value))


def validate_plan(value, limit=5):
    if not isinstance(value, dict) or set(value) - KEYS:
        raise ValueError('invalid query plan keys')
    plan = {}
    for key in ('required', 'preferred', 'excluded'):
        terms = value.get(key, [])
        if not isinstance(terms, list) or len(terms) > 30:
            raise ValueError('invalid terms')
        plan[key] = []
        for term in terms:
            if not isinstance(term, dict) or set(term) - {'kind', 'value', 'weight'} or term.get('kind') not in KINDS | (SOFT_KINDS if key == 'preferred' else set()):
                raise ValueError('invalid term kind or keys')
            if not isinstance(term.get('value'), str) or not 1 <= len(term['value'].strip()) <= 200:
                raise ValueError('invalid term value')
            weight = term.get('weight', 1.0)
            if type(weight) not in (float, int) or not math.isfinite(weight) or not 0 <= weight <= 5:
                raise ValueError('invalid weight')
            plan[key].append({'kind': term['kind'], 'value': term['value'].strip(), **({'weight': weight} if key == 'preferred' else {})})
    pages = value.get('page_range', {})
    if not isinstance(pages, dict) or set(pages) - {'min', 'max', 'hard'} or type(pages.get('hard', False)) is not bool:
        raise ValueError('invalid page range')
    plan['page_range'] = {'min': None, 'max': None, 'hard': pages.get('hard', False)}
    for key in ('min', 'max'):
        if pages.get(key) is not None:
            plan['page_range'][key] = bounded_int(pages[key], 1, 10000)
    if pages.get('min') and pages.get('max') and pages['min'] > pages['max']:
        raise ValueError('page minimum exceeds maximum')
    for key in ('semantic_query', 'visual_query', 'narrative_query'):
        item = value.get(key, '' if key == 'semantic_query' else None)
        if item is not None and (not isinstance(item, str) or len(item) > 4000):
            raise ValueError('invalid query text')
        if key == 'semantic_query' and item is None:
            raise ValueError('semantic_query must be text')
        plan[key] = item
    if value.get('mode', 'fast') not in ('auto', 'fast', 'deep'):
        raise ValueError('invalid mode')
    plan['mode'] = value.get('mode', 'fast')
    count = value.get('requested_count', limit)
    if type(count) is not int:
        raise ValueError('invalid requested_count')
    plan['requested_count'] = max(1, min(count, limit, 5))
    ids = value.get('excluded_gallery_ids', [])
    if not isinstance(ids, list) or len(ids) > 100 or not all(gallery_id(i) for i in ids):
        raise ValueError('invalid excluded gallery IDs')
    plan['excluded_gallery_ids'] = list(dict.fromkeys(ids))
    return plan


def validate_request(value, limit=5):
    if not isinstance(value, dict) or set(value) - {'message', 'mode', 'limit', 'previous_plan'}:
        raise ValueError('invalid request keys')
    message = value.get('message')
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
        raise ValueError('message must contain 1–4000 characters')
    if value.get('mode', 'auto') not in ('auto', 'fast', 'deep'):
        raise ValueError('invalid mode')
    count = value.get('limit', limit)
    if type(count) is not int:
        raise ValueError('limit must be an integer')
    previous = value.get('previous_plan')
    return dict(message=message.strip(), mode=value.get('mode', 'auto'), limit=max(1, min(5, limit, count)),
                previous_plan=validate_plan(previous, limit) if previous is not None else None)
