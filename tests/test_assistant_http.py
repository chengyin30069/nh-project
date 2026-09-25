import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from server.nh_server import DownloadManager, LocalLibrary, make_library_handler, parse_networks
from server.assistant.provider import FakeModelProvider

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = LocalLibrary(DownloadManager(storage_dir=Path(self.tmp.name), autostart=False), cache_autostart=False,
            assistant_config={'enabled': True, 'background_enabled': False}, assistant_provider=FakeModelProvider())
        self.addCleanup(self.library.assistant.close)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_library_handler(self.library, parse_networks(['127.0.0.1/32']), base_path='/nh'))
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f'http://127.0.0.1:{self.httpd.server_port}/nh/_nh-local/api/assistant/'

    def request(self, action, payload=None, headers=None):
        req = urllib.request.Request(self.base+action, data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type': 'application/json'} | (headers or {}))
        try:
            with urllib.request.urlopen(req) as res: return res.status, json.load(res)
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.load(exc)

    def test_health_and_api_limits(self):
        status, data = self.request('health')
        self.assertEqual(status, 200)
        self.assertTrue(data['enabled'])
        self.assertEqual(data["api_key_env"], "NVIDIA_API_KEY")
        self.assertTrue(data["available"])
        self.assertEqual(self.request('recommend', {'message': 'book'})[0], 200)
        self.assertEqual(self.request('recommend', {'message': 'x'*4001})[0], 400)
        self.assertEqual(self.request('recommend', {'message': 'x'*33000})[0], 400)
        self.assertEqual(self.request('recommend', {'message': 'book'}, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('index/metadata', {})[0], 202)
        self.assertEqual(self.request('index/visual', {})[0], 400)
        self.assertEqual(self.request('jobs/unknown')[0], 404)

    def test_disabled_keeps_health(self):
        self.library.assistant.close()
        self.library.assistant = None
        self.library.assistant_config = {"enabled": False}
        status, data = self.request('health')
        self.assertEqual(status, 200)
        self.assertFalse(data['enabled'])
        self.assertEqual(self.request('recommend', {'message': 'book'})[0], 503)

    def test_enabled_initialization_failure_is_not_disabled(self):
        from unittest.mock import patch
        with patch('server.assistant.service.AssistantService', side_effect=ModuleNotFoundError('secret-must-not-be-shown')):
            library = LocalLibrary(DownloadManager(storage_dir=Path(self.tmp.name), autostart=False), cache_autostart=False,
                env={'NVIDIA_API_KEY': 'secret-must-not-be-shown'}, assistant_config={'enabled': True})
        data = library.assistant_health()
        self.assertTrue(data['enabled'])
        self.assertTrue(data['api_key_configured'])
        self.assertFalse(data['available'])
        self.assertEqual(data['error']['code'], 'missing_dependency')
        self.assertNotIn('secret-must-not-be-shown', json.dumps(data))

    def test_connection_check_missing_verified_and_unauthorized(self):
        from server.assistant.provider import ProviderError
        provider = self.library.assistant.provider
        provider.configured = False
        self.assertEqual(self.request('check', {})[1]['state'], 'missing_key')
        provider.configured = True
        self.assertEqual(self.request('check', {})[1]['state'], 'verified')
        provider.embed_texts = lambda **kw: (_ for _ in ()).throw(ProviderError('secret-must-not-be-shown', status=401))
        status, data = self.request('check', {})
        self.assertEqual(data['code'], 'authentication_failed')
        self.assertNotIn('secret-must-not-be-shown', json.dumps(data))
        self.assertEqual(self.request('health')[1]['connection_check']['state'], 'failed')
