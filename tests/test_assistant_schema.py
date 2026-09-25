import unittest
from server.assistant.schema import validate_plan, validate_request, json_object
from server.assistant.settings import settings

class SchemaTests(unittest.TestCase):
    def test_strict_schema(self):
        for value in [{'oops': 1}, {'required': [{'kind': 'mood', 'value': 'calm'}]}, {'page_range': {'min': 100, 'max': 20}}, {'page_range': {'max': True}}, {'page_range': {'max': 10001}}, {'excluded_gallery_ids': ['../9']}, {'preferred': [{'kind': 'tag', 'value': 'a', 'weight': float('nan')}]}, {'semantic_query': []}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_plan(value)
        self.assertEqual(validate_plan({'requested_count': 100})['requested_count'], 5)
        self.assertEqual(json_object('```json\n{"required": []}\n```'), {'required': []})
        for raw in ['oops', '[]']:
            with self.assertRaises(ValueError): json_object(raw)

    def test_request_and_previous(self):
        request = validate_request({'message': 'shorter', 'limit': 99, 'previous_plan': {'excluded_gallery_ids': ['9']}})
        self.assertEqual(request['limit'], 5)
        self.assertEqual(request['previous_plan']['excluded_gallery_ids'], ['9'])
        for value in [{'message': ''}, {'message': 'a'*4001}, {'message': 'ok', 'previous_plan': {'unknown': []}}, {'message': 'ok', 'limit': True}, {'message': 'ok', 'mode': 'oops'}]:
            with self.assertRaises(ValueError): validate_request(value)

    def test_config(self):
        for value in [{'api_key': 'secret'}, {'enabled': 'yes'}, {'max_retries_interactive': -1}, {'api_base': 'http://example.com'}, {'result_limit': 6}, {'max_remote_concurrency': 0}]:
            with self.assertRaises(ValueError): settings(value)
