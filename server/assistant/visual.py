"""Validated visual observations and canonical retrieval text."""
import hashlib
import json

from .schema import json_object

FIELDS = ('style', 'setting', 'visible_characters', 'activities', 'tone', 'composition', 'warnings')
SCHEMA_VERSION = 'visual-json-v1'
PROMPT = ('Observe only these sampled comic pages. Return one JSON object with list-of-string keys '
          'style, setting, visible_characters, activities, tone, composition, warnings. '
          'At most eight short observations per key. Describe visible details only; do not guess '
          'character identities, story arcs, ending, or unsampled pages. No markdown.')


def namespace(model):
    return hashlib.sha256(f'{model}:{SCHEMA_VERSION}:sparse-v1:{PROMPT}'.encode()).hexdigest()[:24]


def observation(value):
    if isinstance(value, str):
        value = json_object(value)
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise ValueError('invalid_analysis_json')
    result = {}
    for field in FIELDS:
        entries = value.get(field, [])
        if not isinstance(entries, list) or len(entries) > 8:
            raise ValueError('invalid_analysis_json')
        if any(not isinstance(entry, str) or not 1 <= len(entry.strip()) <= 200 for entry in entries):
            raise ValueError('invalid_analysis_json')
        result[field] = [entry.strip() for entry in entries]
    text = '\n'.join(f'{field}: {"; ".join(result[field])}' for field in FIELDS if result[field])
    if not text or len(text.encode('utf-8')) > 6000:
        raise ValueError('invalid_analysis_json')
    return result, text, hashlib.sha256(text.encode()).hexdigest()
