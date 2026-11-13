import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

import server


class ClientAIContextTests(unittest.TestCase):
    def payload(self):
        return dict(status='ok', client_id=12, company_profile='Profil', product_description='Oferta',
                    target_group='Firmy', language='pl', whitelist_ai_prompt='  Prompt\nAI  ')

    def test_tool_exists(self):
        self.assertTrue(callable(getattr(server, 'get_client_ai_context', None)))

    def test_preserves_configuration_and_drops_extra_fields(self):
        payload = self.payload()
        with patch('server.get_json', return_value={**payload, 'password': 'must-not-leak'}) as api:
            self.assertEqual(server.get_client_ai_context(12), payload)
        api.assert_called_once_with('/api/clients/12/ai-context')

    def test_invalid_ids_never_fetch(self):
        with patch('server.get_json') as api:
            for value in (0, -1, True):
                with self.subTest(value=value), self.assertRaises(ToolError):
                    server.get_client_ai_context(value)
        api.assert_not_called()

    def test_malformed_or_wrong_client_response(self):
        good = self.payload()
        for payload in ([], {}, {**good, 'client_id': 13}, {**good, 'status': 'error'},
                        {**good, 'company_profile': None}, {**good, 'language': []},
                        {k: v for k, v in good.items() if k != 'whitelist_ai_prompt'}):
            with self.subTest(payload=payload), patch('server.get_json', return_value=payload):
                with self.assertRaises(ToolError):
                    server.get_client_ai_context(12)

    def test_permission_and_missing_endpoint_errors_are_preserved(self):
        for message in ('Access denied.', 'endpoint returned HTTP 404.'):
            with patch('server.get_json', side_effect=ToolError(message)):
                with self.assertRaisesRegex(ToolError, message):
                    server.get_client_ai_context(12)
