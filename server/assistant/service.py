"""Optional assistant lifecycle facade used by the existing HTTP server."""
import os
import sqlite3
import time
from .settings import settings
from .diagnostics import provider_error
from .provider import ProviderError
from .db import AssistantDatabase
from .indexer import AssistantIndexer
from .nim_client import NvidiaNimClient
from .recommender import Recommender
from .vector_index import VectorIndex


class AssistantService:
    def __init__(self, library, config, env=None, provider=None):
        self.config = settings(config)
        self.library = library
        self.enabled = self.config['enabled']
        self.connection_check = {'state': 'not_checked'}
        self.provider = self.db = self.indexer = None
        if not self.enabled:
            return
        path = library.path.parent / 'assistant.sqlite3'
        try:
            self.db = AssistantDatabase(path)
        except sqlite3.DatabaseError as exc:
            if getattr(exc, 'sqlite_errorcode', None) not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            # Quarantine only the disposable sidecar, never library.sqlite3.
            for suffix in ('', '-wal', '-shm'):
                source = path.with_name(path.name + suffix)
                if source.exists():
                    source.rename(source.with_name(source.name + f'.corrupt-{time.time_ns()}'))
            self.db = AssistantDatabase(path)
        self.provider = provider or NvidiaNimClient(self.config, os.environ if env is None else env)
        self.vectors = VectorIndex(self.db, self.config['embedding_model'])
        self.indexer = AssistantIndexer(library, self.db, self.provider, self.config, self.vectors)
        self.recommender = Recommender(library, self.provider, self.config, self.vectors)
        library.assistant_change_callback = self.indexer.enqueue
        library.assistant_delete_callback = self.indexer.delete
        if self.db.model('embedding', self.config['embedding_model']):
            self.indexer.scan()
        elif self.db.state('metadata_scan_pending') == '1':
            self.indexer.scan(resume=True)
        for role in ('parser', 'quality'):
            self.db.model(role, self.config[role + '_model'])

    def health(self):
        total = self.library.assistant_count()
        return dict(enabled=self.enabled, available=True, error=getattr(self.provider, "last_error", None),
                    connection_check=self.connection_check, api_key_configured=bool(self.provider and self.provider.configured),
                    provider_state=self.provider.state if self.provider else 'disabled',
                    metadata_index=(self.db.status(self.config['embedding_model']) if self.db else dict(indexed=0, model=self.config['embedding_model'])) | {'total': total},
                    visual_index=dict(indexed=0, total=total, enabled=False),
                    background_enabled=self.config['background_enabled'],
                    scanning=bool(self.indexer and self.indexer.scan_lock.locked()))

    def check_connection(self):
        if not self.provider or not self.provider.configured:
            self.connection_check = {'state': 'missing_key', 'message': 'API key was not found in the server environment. Set the configured variable and recreate the container.'}
        else:
            try:
                self.provider.embed_texts(model=self.config['embedding_model'], texts=['Connection check.'], input_type='query', purpose='query')
                self.connection_check = {'state': 'verified', 'message': 'NIM accepted the key and the configured embedding model responded.', 'model': self.config['embedding_model']}
            except ProviderError as exc:
                self.connection_check = {'state': 'failed', **provider_error(exc)}
        self.connection_check['checked_at'] = time.time()
        return self.connection_check

    def recommend(self, payload):
        if not self.enabled:
            raise ValueError('Assistant is disabled in server configuration.')
        return self.recommender.recommend(payload)

    def index(self, action):
        if not self.enabled:
            raise ValueError('Assistant is disabled in server configuration.')
        if action == 'metadata':
            return {'scanning': self.indexer.scan()}
        if action == 'retry-failed':
            count = self.db.retry_failed(self.config['embedding_model'])
            self.indexer.wake.set()
            return {'queued': count}
        if action.startswith('gallery/'):
            from .schema import gallery_id
            gid = action.removeprefix('gallery/')
            if not gallery_id(gid) or not self.library.assistant_gallery(gid):
                raise ValueError('Downloaded gallery not found.')
            return {'queued': self.indexer.enqueue(gid)}
        raise ValueError('Index action is not available in V1.')

    def close(self):
        self.library.assistant_change_callback = None
        self.library.assistant_delete_callback = None
        if self.indexer:
            self.indexer.close()
        if self.provider:
            self.provider.close()
