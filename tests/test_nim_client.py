import io
import json
import threading
import unittest
import urllib.error
from server.assistant.nim_client import NvidiaNimClient, RemoteInferenceScheduler, retry_after
from server.assistant.provider import ProviderError
from server.assistant.settings import settings

class Response(io.BytesIO):
    status = 200

class NimTests(unittest.TestCase):
    def client(self, opener, retries=0):
        client = NvidiaNimClient(settings({'min_request_interval_ms': 0, 'max_retries_interactive': retries}), {'NVIDIA_API_KEY': 'fixture-secret'}, opener=opener)
        self.addCleanup(client.close)
        return client

    def test_embedding_payload_auth_order(self):
        captured = []
        def opener(req, **kwargs):
            captured.append(req)
            return Response(json.dumps({'data': [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]}).encode())
        client = self.client(opener)
        result = client.embed_texts(model='m', texts=['a', 'b'], input_type='passage', purpose='embed_metadata')
        self.assertEqual(result, [[1, 0], [0, 1]])
        self.assertEqual(captured[0].headers['Authorization'], 'Bearer fixture-secret')
        payload = json.loads(captured[0].data)
        self.assertEqual(payload['input_type'], 'passage')
        self.assertEqual(payload['truncate'], 'END')

    def test_error_mapping_no_secret(self):
        for status in (400, 401, 429, 503):
            def opener(req, **kwargs):
                raise urllib.error.HTTPError(req.full_url, status, 'fixture-secret', {'Retry-After': '12'}, io.BytesIO(b'fixture-secret'))
            client = self.client(opener)
            with self.assertRaises(ProviderError) as context:
                client.embed_texts(model='m', texts=['a'], input_type='query', purpose='query')
            self.assertNotIn('fixture-secret', str(context.exception))
            self.assertEqual(context.exception.transient, status in (429, 503))
            self.assertEqual(context.exception.retry_after, 12)
        self.assertEqual(retry_after('garbage'), 0)

    def test_timeout_and_circuit(self):
        client = self.client(lambda *a, **kw: (_ for _ in ()).throw(TimeoutError()))
        for _ in range(5):
            with self.assertRaises(ProviderError): client.embed_texts(model='m', texts=['x'], input_type='query', purpose='query')
        self.assertEqual(client.state, 'degraded')
        with self.assertRaises(ProviderError) as context:
            client.embed_texts(model='m', texts=['x'], input_type='query', purpose='query')
        self.assertEqual(context.exception.code, 'provider_degraded')

    def test_priority_after_current_request(self):
        scheduler = RemoteInferenceScheduler(settings({'min_request_interval_ms': 0}))
        self.addCleanup(scheduler.close)
        entered, release = threading.Event(), threading.Event()
        order = []
        def initial(_): entered.set(); release.wait(2); order.append('initial')
        threads = [threading.Thread(target=lambda: scheduler.submit(initial, 'embed_metadata'))]
        threads[0].start(); self.assertTrue(entered.wait(1))
        for name, purpose in [('background', 'embed_metadata'), ('interactive', 'parse')]:
            thread = threading.Thread(target=lambda n=name, p=purpose: scheduler.submit(lambda _: order.append(n), p))
            threads.append(thread); thread.start()
        # Wait on scheduler condition rather than racing thread creation.
        import time
        deadline = time.monotonic()+2
        while len(scheduler.queue) < 2 and time.monotonic() < deadline: time.sleep(.005)
        release.set()
        for thread in threads: thread.join(2)
        self.assertEqual(order, ['initial', 'interactive', 'background'])

    def test_visual_payload_is_bounded_and_ordered(self):
        captured = []
        def opener(req, **kwargs):
            captured.append((req, kwargs))
            return Response(json.dumps({'choices': [{'message': {'content': '{"style":["ink"]}'}}]}).encode())
        client = self.client(opener)
        images = [{'type': 'text', 'text': 'Page 1'}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,AA=='}},
                  {'type': 'text', 'text': 'Page 2'}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,BB=='}}]
        result = client.visual_chat(model='z-ai/glm-5.3-flash', messages=[{'role': 'user', 'content': images}])
        self.assertIn('ink', result.text)
        payload = json.loads(captured[0][0].data)
        self.assertEqual(payload['messages'][0]['content'], images)
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['chat_template_kwargs'], {'clear_thinking': True})
        self.assertLessEqual(captured[0][1]['timeout'], 180)
        with self.assertRaises(ValueError):
            client.visual_chat(model='m', messages=[{'role': 'user', 'content': []}])
