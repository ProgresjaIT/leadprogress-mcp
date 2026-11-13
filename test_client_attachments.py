import asyncio
import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError

import server


class ClientAttachmentTests(unittest.TestCase):
    def test_registered_as_read_tool_with_required_client(self):
        tools = asyncio.run(server.mcp.list_tools())
        tool = next((tool for tool in tools if tool.name == 'list_client_attachments'), None)
        self.assertIsNotNone(tool)
        self.assertEqual(tool.inputSchema['required'], ['client_id'])

    def test_search_pagination_and_public_metadata(self):
        rows = [
            {'id': 3, 'filename': 'c.pdf', 'description': 'Umowa C', 'type': 'out_going_to_client'},
            {'id': 1, 'filename': 'umowa.pdf', 'description': '', 'type': 'out_going_to_client',
             'mime_type': 'application/pdf', 'size': 123, 'file_path': '/private/file'},
            {'id': 2, 'filename': 'b.pdf', 'description': 'Umowa B', 'type': 'incoming'},
        ]
        with patch('server._list_clients', return_value=[{'client_id': 12}]), \
                patch('server.get_json', return_value={'attachments': rows}) as api:
            first = server.list_client_attachments(12, query=' UMOWA ', limit=1)
            second = server.list_client_attachments(12, query=' UMOWA ', limit=1, offset=first['next_offset'])
        self.assertEqual(first['client_id'], 12)
        self.assertEqual(first['total'], 2)
        self.assertEqual(first['returned'], 1)
        self.assertEqual(first['items'][0]['id'], 1)
        self.assertEqual(first['items'][0]['size'], 123)
        self.assertNotIn('file_path', first['items'][0])
        self.assertEqual(second['items'][0]['id'], 3)
        self.assertIsNone(second['next_offset'])
        self.assertTrue(all(call.args == ('/proxy/clients/12/available_attachments?type=out_going_to_client',)
                            for call in api.call_args_list))

    def test_empty_library(self):
        with patch('server._list_clients', return_value=[{'client_id': 12}]), \
                patch('server.get_json', return_value={'attachments': []}):
            result = server.list_client_attachments(12)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['total'], 0)
        self.assertIsNone(result['next_offset'])

    def test_invalid_input_before_network(self):
        with patch('server._list_clients') as clients, patch('server.get_json') as api:
            for args in ({'client_id': 0}, {'client_id': -1}, {'client_id': True},
                         {'client_id': 12, 'limit': 101}, {'client_id': 12, 'offset': -1}):
                with self.subTest(args=args), self.assertRaises(ToolError):
                    server.list_client_attachments(**args)
        clients.assert_not_called()
        api.assert_not_called()

    def test_inaccessible_client_never_fetches_library(self):
        with patch('server._list_clients', return_value=[{'client_id': 13}]), \
                patch('server.get_json') as api:
            with self.assertRaisesRegex(ToolError, 'accessible'):
                server.list_client_attachments(12)
        api.assert_not_called()

    def test_malformed_library_rejected(self):
        item = {'id': 1, 'filename': 'x.pdf', 'type': 'out_going_to_client'}
        for payload in ([], {}, {'attachments': None}, {'attachments': [None]},
                        {'attachments': [dict(item, id=True)]},
                        {'attachments': [dict(item, filename=' ')]},
                        {'attachments': [dict(item, description=123)]},
                        {'attachments': [item, item]}):
            with self.subTest(payload=payload), \
                    patch('server._list_clients', return_value=[{'client_id': 12}]), \
                    patch('server.get_json', return_value=payload):
                with self.assertRaises(ToolError):
                    server.list_client_attachments(12)
