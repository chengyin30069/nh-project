import threading
import time
import unittest
from server.assistant.requests import RequestQueue
from server.assistant.nim_client import RemoteInferenceScheduler, NvidiaNimClient
from server.assistant.settings import settings
from server.assistant.provider import ProviderError


class RequestTests(unittest.TestCase):
    def test_nonblocking_progress_capacity_and_failure(self):
        release = threading.Event()
        def run(payload, progress):
            progress('semantic')
            release.wait(2)
            if payload.get('fail'):
                raise ValueError('secret')
            return {'results': []}
        jobs = RequestQueue(run, capacity=1, ttl=0)
        self.addCleanup(jobs.close)
        self.addCleanup(release.set)
        started = time.monotonic()
        job = jobs.submit({})
        self.assertLess(time.monotonic()-started, .2)
        with self.assertRaises(ValueError): jobs.submit({})
        self.assertIsNone(jobs.get('unknown'))
        release.set()
        # Expired completed jobs are removed, while running jobs remain pollable.
        for _ in range(100):
            if jobs.get(job['job_id']) is None: break
            time.sleep(.01)
        self.assertIsNone(jobs.get(job['job_id']))
        jobs.ttl = 900
        job = jobs.submit({'fail': True})
        for _ in range(100):
            result = jobs.get(job['job_id'])
            if result['status'] == 'failed': break
            time.sleep(.01)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('secret', result['error'])

    def test_scheduler_queue_deadline_removes_unsent_request(self):
        scheduler = RemoteInferenceScheduler(settings({'min_request_interval_ms': 0}))
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(scheduler.close)
        self.addCleanup(release.set)
        def hold(_): entered.set(); release.wait(2)
        thread = threading.Thread(target=lambda: scheduler.submit(hold, 'embed_metadata'))
        thread.start(); entered.wait(1)
        calls = []
        with self.assertRaises(ProviderError):
            scheduler.submit(lambda _: calls.append('sent'), 'parse', deadline=time.monotonic()+.02)
        release.set(); thread.join(1)
        self.assertEqual(calls, [])
        self.assertTrue(scheduler.worker.is_alive())

    def test_sent_timeout_does_not_stop_worker_or_retry(self):
        scheduler = RemoteInferenceScheduler(settings({'min_request_interval_ms': 0}))
        release = threading.Event()
        self.addCleanup(scheduler.close)
        self.addCleanup(release.set)
        with self.assertRaises(ProviderError):
            scheduler.submit(lambda _: release.wait(2), 'parse', deadline=time.monotonic()+.02)
        release.set()
        self.assertEqual(scheduler.submit(lambda _: 'next', 'parse'), 'next')

    def test_nim_nonreasoning_and_interactive_timeout(self):
        import io, json
        captured = []
        def opener(req, timeout):
            captured.append((json.loads(req.data), timeout))
            response = io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
            response.status = 200
            return response
        config = settings({'min_request_interval_ms': 0})
        client = NvidiaNimClient(config, {'NVIDIA_API_KEY': 'test'}, opener=opener)
        self.addCleanup(client.close)
        with client.interactive_budget():
            client.chat(model=config['parser_model'], messages=[], max_tokens=600, temperature=.1, purpose='parse')
        self.assertFalse(captured[0][0]['chat_template_kwargs']['enable_thinking'])
        self.assertLessEqual(captured[0][1], 20)

    def test_whole_interactive_budget_applies_across_calls(self):
        import io
        def opener(req, timeout):
            time.sleep(.025)
            response = io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}')
            response.status = 200
            return response
        client = NvidiaNimClient(settings({'min_request_interval_ms': 0}), {'NVIDIA_API_KEY': 'test'}, opener=opener)
        self.addCleanup(client.close)
        client.config['interactive_timeout_seconds'] = .1
        client.config['interactive_budget_seconds'] = .04
        start = time.monotonic()
        with client.interactive_budget():
            client.chat(model='m', messages=[], max_tokens=10, temperature=.1, purpose='parse')
            with self.assertRaises(ProviderError):
                client.chat(model='m', messages=[], max_tokens=10, temperature=.1, purpose='parse')
        self.assertLess(time.monotonic()-start, .09)
