import asyncio
import json
from pathlib import Path
import sys
import unittest

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import server


class ReplyPromptTests(unittest.TestCase):
    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        self.calls = []
        self.clients = {'Client': dict(client_id=12, is_active=True, in_progress=True, is_paused=False)}
        self.payload = dict(status='ok', description_lead='  Wytyczne\nLEAD  ',
                            description_contracting='Umowa', description_active='', stage=None,
                            internal_secret='must-not-leak')
        self.status = 200
        server._client = httpx.Client(base_url='https://panel.example', transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        self.calls.append((request.method, str(request.url)))
        self.assertEqual(request.method, 'GET')
        if request.url.path == '/api/statistics':
            return httpx.Response(200, json=self.clients)
        self.assertEqual(request.url.path, '/proxy/get_agent_info')
        self.assertEqual(dict(request.url.params), {'client_id': '12'})
        return httpx.Response(self.status, json=self.payload)

    def test_tool_exists(self):
        self.assertTrue(callable(getattr(server, 'get_reply_prompts', None)))

    def test_returns_exact_dashboard_text_and_only_allowed_fields(self):
        self.assertEqual(server.get_reply_prompts(12), {
            'status': 'ok', 'client_id': 12, 'description_lead': '  Wytyczne\nLEAD  ',
            'description_contracting': 'Umowa', 'description_active': '',
        })
        self.assertEqual(len(self.calls), 2)

    def test_invalid_id_does_not_contact_panel(self):
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaises(ToolError):
                server.get_reply_prompts(value)
        self.assertEqual(self.calls, [])

    def test_unavailable_client_does_not_fetch_prompts(self):
        self.clients = {}
        with self.assertRaisesRegex(ToolError, 'accessible'):
            server.get_reply_prompts(12)
        self.assertEqual(len(self.calls), 1)

    def test_missing_configuration_is_not_misreported_as_empty_prompts(self):
        self.status = 404
        with self.assertRaisesRegex(ToolError, '404'):
            server.get_reply_prompts(12)

    def test_rejects_error_or_incomplete_payload(self):
        good = self.payload.copy()
        for payload in ([], {}, {**good, 'status': 'error'},
                        {**good, 'description_lead': None}, {**good, 'description_active': []},
                        {k: v for k, v in good.items() if k != 'description_contracting'}):
            self.payload = payload
            with self.subTest(payload=payload), self.assertRaises(ToolError):
                server.get_reply_prompts(12)


class ReplyPromptMCPTest(unittest.TestCase):
    def test_read_through_stdio(self):
        async def run():
            script = ('import sys; sys.path.insert(0, ' + repr(str(Path(server.__file__).parent)) + '); '
                      'from tests.test_reply_prompts import ReplyPromptTests; '
                      'import server; fixture=ReplyPromptTests(); fixture.setUp(); server.mcp.run()')
            params = StdioServerParameters(command=sys.executable, args=['-B', '-c', script])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool('get_reply_prompts', {'client_id': 12})
                    self.assertFalse(result.isError, str(result.content))
                    data = result.structuredContent or json.loads(result.content[0].text)
                    self.assertEqual(data['description_lead'], '  Wytyczne\nLEAD  ')
                    self.assertEqual(data['description_active'], '')
                    self.assertNotIn('internal_secret', data)
        asyncio.run(asyncio.wait_for(run(), 30))
