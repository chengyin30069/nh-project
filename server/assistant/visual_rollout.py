"""One-time, restart-safe transition from an authorized visual pilot to full scan."""
import json
import os
import time
from pathlib import Path

import yaml

from .db import AssistantDatabase
from .settings import settings
from .visual import namespace


def advance(db, pilot_id, current_namespace):
    """Return the full operation, or None while the pilot is still running."""
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        pilot = connection.execute('SELECT scope,status,discovered,namespace FROM assistant_operations WHERE operation_id=?',
                                   (pilot_id,)).fetchone()
        if not pilot or pilot['scope'] != 'ids' or not 20 <= pilot['discovered'] <= 50 or pilot['namespace'] != current_namespace:
            raise ValueError('The configured current-model 20–50 gallery pilot was not found.')
        existing = connection.execute("""SELECT operation_id FROM assistant_operations
            WHERE scope='all' AND namespace=? AND status!='cancelled' ORDER BY created_at LIMIT 1""",
            (current_namespace,)).fetchone()
        if existing:
            return existing[0]
        if pilot['status'] != 'completed':
            return None
        searchable = connection.execute("""SELECT count(*) FROM assistant_operation_items
            WHERE operation_id=? AND status='searchable'""", (pilot_id,)).fetchone()[0]
        if searchable < 20:
            raise ValueError(f'Pilot completed with only {searchable} searchable summaries; full scan remains locked.')
        verified = 0
        for row in connection.execute("""SELECT s.evidence_json,d.text FROM assistant_operation_items i
            JOIN assistant_document_sources s ON s.gallery_id=i.gallery_id AND s.kind='visual' AND s.producer_namespace=?
            JOIN assistant_documents d ON d.gallery_id=i.gallery_id AND d.kind='visual' AND d.producer_version=?
            WHERE i.operation_id=? AND i.status='searchable'""",
            (current_namespace, current_namespace, pilot_id)):
            evidence = json.loads(row['evidence_json'])
            observation = evidence.get('observation', {})
            if (evidence.get('pages') and len(row['text'].strip()) >= 50
                    and sum(bool(observation.get(field)) for field in ('style', 'setting', 'visible_characters', 'activities', 'tone', 'composition')) >= 2):
                verified += 1
        if verified < 20:
            raise ValueError(f'Only {verified} pilot summaries passed the basic evidence-quality review; full scan remains locked.')
        connection.execute("INSERT OR REPLACE INTO assistant_library_state(key,value) VALUES ('visual_pilot_approved',?)",
                           (current_namespace,))
        import uuid
        operation_id = uuid.uuid4().hex
        now = time.time()
        connection.execute("""INSERT INTO assistant_operations
            (operation_id,operation_type,scope,status,cursor,discovered,namespace,created_at,updated_at)
            VALUES (?,'visual_index','all','queued',0,0,?,?,?)""", (operation_id, current_namespace, now, now))
        print(f'Pilot approved: searchable={searchable}, reviewed={verified}; full operation={operation_id}', flush=True)
        return operation_id


def main():
    pilot_id = os.environ['NH_VISUAL_PILOT_OPERATION_ID']
    config = settings(yaml.safe_load(Path('/app/config.yaml').read_text())['assistant'])
    current_namespace = namespace(config['visual_model'])
    db = AssistantDatabase(Path('/home/nh/nh/.nh-local/assistant.sqlite3'))
    last_report = 0
    while True:
        try:
            operation_id = advance(db, pilot_id, current_namespace)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f'Full visual scan remains locked: {exc}', flush=True)
            time.sleep(300)
            continue
        if operation_id:
            if time.time() - last_report >= 3600:
                full = db.operation(operation_id)
                print(f"Full visual scan: id={operation_id} status={full['status']} discovered={full['discovered']} counts={full['counts']} errors={full['errors']}", flush=True)
                last_report = time.time()
            time.sleep(60)
        else:
            if time.time() - last_report >= 300:
                pilot = db.operation(pilot_id)
                print(f"Visual pilot: status={pilot['status']} counts={pilot['counts']} errors={pilot['errors']}", flush=True)
                last_report = time.time()
            time.sleep(30)


if __name__ == '__main__':
    main()
