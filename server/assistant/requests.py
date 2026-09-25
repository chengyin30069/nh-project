"""Bounded, short-lived recommendation requests; HTTP connections never wait on NIM."""
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor


class RequestQueue:
    def __init__(self, run, *, capacity=2, ttl=900):
        self.run, self.capacity, self.ttl = run, capacity, ttl
        self.lock = threading.Lock()
        self.jobs = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='nh-recommend')
        self.closed = False

    def _expire(self):
        cutoff = time.time()-self.ttl
        self.jobs = {key: value for key, value in self.jobs.items() if value['updated_at'] > cutoff or value['status'] not in ('ready', 'failed')}

    def submit(self, payload):
        with self.lock:
            self._expire()
            if self.closed or sum(j['status'] not in ('ready', 'failed') for j in self.jobs.values()) >= self.capacity:
                raise ValueError('Assistant is busy. Please wait for the current requests to finish.')
            # Bound retained results as well as pending work.
            if len(self.jobs) >= 64:
                terminal = [key for key, value in self.jobs.items() if value['status'] in ('ready', 'failed')]
                if terminal:
                    del self.jobs[terminal[0]]
            job_id = uuid.uuid4().hex
            job = dict(job_id=job_id, request_id=job_id, status='queued', stage='queued', created_at=time.time(), updated_at=time.time())
            self.jobs[job_id] = job
            result = copy.deepcopy(job)
            self.executor.submit(self._work, job_id, copy.deepcopy(payload))
            return result

    def _work(self, job_id, payload):
        def progress(stage):
            with self.lock:
                self.jobs[job_id].update(status='processing', stage=stage, updated_at=time.time())
        try:
            result = self.run(payload, progress=progress)
        except Exception:
            with self.lock:
                self.jobs[job_id].update(status='failed', error='Recommendation failed. Please retry.', updated_at=time.time())
        else:
            with self.lock:
                self.jobs[job_id].update(status='ready', stage='ready', result=result, updated_at=time.time())

    def get(self, job_id):
        with self.lock:
            self._expire()
            return copy.deepcopy(self.jobs.get(job_id))

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
