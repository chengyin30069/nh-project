"""Canonical metadata, deliberately excluding paths and other private fields."""
import hashlib

VERSION = 'metadata-v1'
FIELDS = [('parody', 'Parodies'), ('character', 'Characters'), ('artist', 'Artists'),
          ('group', 'Groups'), ('tag', 'Tags'), ('language', 'Languages'), ('category', 'Categories')]


def document(record):
    lines = [f"Gallery ID: {record['id']}"]
    for field, label in [('title_english', 'English title'), ('title_japanese', 'Japanese title'), ('title_pretty', 'Pretty title'), ('num_pages', 'Pages')]:
        lines.append(f"{label}: {' '.join(str(record.get(field) or '').split())}")
    for kind, label in FIELDS:
        names = sorted({str(t['name']).strip() for t in record['tags'] if t['type'] == kind})
        lines.append(f"{label}: {'; '.join(names)}")
    # UTF-8 byte bound is conservative even for multilingual tokenization.
    text = '\n'.join(lines).encode('utf-8')[:6000].decode('utf-8', errors='ignore')
    return text, hashlib.sha256(text.encode()).hexdigest()


def compact(record):
    return dict(id=str(record['id']), title=str(record['title'])[:500], pages=record.get('num_pages'),
                tags=[dict(type=t['type'], name=t['name'][:200]) for t in record['tags'][:80]])
