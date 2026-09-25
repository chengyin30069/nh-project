import os
import unittest
from server.assistant.nim_client import NvidiaNimClient
from server.assistant.settings import settings

@unittest.skipUnless(os.environ.get('NH_RUN_NIM_SMOKE') == '1' and os.environ.get('NVIDIA_API_KEY'), 'explicit live NIM opt-in required')
class NimLiveTests(unittest.TestCase):
    def test_embedding_and_chat(self):
        config = settings()
        client = NvidiaNimClient(config, os.environ)
        self.addCleanup(client.close)
        vectors = client.embed_texts(model=config['embedding_model'], texts=['A quiet library.'], input_type='query', purpose='query')
        self.assertEqual(len(vectors), 1)
        self.assertGreater(len(vectors[0]), 0)
        result = client.chat(model=config['parser_model'], messages=[{'role': 'user', 'content': 'Reply with the word OK.'}], max_tokens=64, temperature=.1, purpose='parse')
        self.assertTrue(result.text)
