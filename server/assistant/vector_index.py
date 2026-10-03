"""Atomically published immutable NumPy snapshots."""
import threading
import numpy as np


class VectorIndex:
    def __init__(self, db, model):
        self.db, self.model = db, model
        self.lock = threading.Lock()
        self.snapshot = (-1, np.array([], dtype=np.int64), np.empty((0, 0), dtype=np.float32))

    def refresh(self):
        if self.snapshot[0] == self.db.generation():
            return
        with self.lock:
            generation, rows = self.db.embeddings(self.model)
            dimensions = {r['dim'] for r in rows}
            if len(dimensions) > 1:
                raise ValueError('mixed embedding dimensions')
            ids, vectors = [], []
            for row in rows:
                v = np.frombuffer(row['vector'], dtype='<f4')
                norm = np.linalg.norm(v)
                if v.size != row['dim'] or not np.isfinite(v).all() or not norm:
                    continue
                ids.append(row['gallery_id'])
                vectors.append(v / norm)
            matrix = np.ascontiguousarray(vectors, dtype=np.float32) if vectors else np.empty((0, 0), dtype=np.float32)
            matrix.flags.writeable = False
            self.snapshot = (generation, np.asarray(ids, dtype=np.int64), matrix)

    def search(self, vector, *, allowed=None, limit=80):
        self.refresh()
        _, ids, matrix = self.snapshot
        if not len(ids):
            return []
        query = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(query)
        if query.shape != (matrix.shape[1],) or not np.isfinite(query).all() or not norm:
            raise ValueError('invalid query vector')
        scores = matrix @ (query / norm)
        positions = np.flatnonzero(np.isin(ids, list(allowed))) if allowed is not None else np.arange(len(ids))
        k = min(limit, len(positions))
        if not k:
            return []
        best = positions[np.argpartition(scores[positions], -k)[-k:]]
        return sorted([(int(ids[i]), float(scores[i])) for i in best], key=lambda pair: (-pair[1], pair[0]))


class VisualVectorIndex:
    def __init__(self, db, model, namespace):
        self.db, self.model, self.namespace = db, model, namespace
        self.lock = threading.Lock()
        self.snapshot = (-1, np.array([], dtype=np.int64), np.empty((0, 0), dtype=np.float32), {})

    def refresh(self):
        if self.snapshot[0] == self.db.visual_generation():
            return
        with self.lock:
            generation, rows = self.db.visual_embeddings(self.model, self.namespace)
            dimensions = {r['dim'] for r in rows}
            if len(dimensions) > 1:
                raise ValueError('mixed visual embedding dimensions')
            ids, vectors, evidence = [], [], {}
            for row in rows:
                vector = np.frombuffer(row['vector'], dtype='<f4')
                norm = np.linalg.norm(vector)
                if vector.size != row['dim'] or not np.isfinite(vector).all() or not norm:
                    continue
                gid = int(row['gallery_id'])
                ids.append(gid)
                vectors.append(vector / norm)
                evidence[gid] = dict(document_key=row['document_key'], source_fingerprint=row['source_fingerprint'],
                                     evidence_json=row['evidence_json'])
            matrix = np.ascontiguousarray(vectors, dtype=np.float32) if vectors else np.empty((0, 0), dtype=np.float32)
            matrix.flags.writeable = False
            self.snapshot = generation, np.asarray(ids, dtype=np.int64), matrix, evidence

    def search(self, vector, *, allowed=None, limit=80):
        self.refresh()
        _, ids, matrix, _ = self.snapshot
        if not len(ids):
            return []
        query = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(query)
        if query.shape != (matrix.shape[1],) or not np.isfinite(query).all() or not norm:
            raise ValueError('invalid query vector')
        scores = matrix @ (query / norm)
        positions = np.flatnonzero(np.isin(ids, list(allowed))) if allowed is not None else np.arange(len(ids))
        k = min(limit, len(positions))
        if not k:
            return []
        best = positions[np.argpartition(scores[positions], -k)[-k:]]
        return sorted([(int(ids[i]), float(scores[i])) for i in best], key=lambda pair: (-pair[1], pair[0]))
