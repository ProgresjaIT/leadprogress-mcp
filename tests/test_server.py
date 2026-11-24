import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import server


class FakePanel(BaseHTTPRequestHandler):
    status = 200
    payload = {'Klient A': {'client_id': 12, 'sent': 100}, 'Klient B': {'client_id': 7}}
    login_ok = True
    requests = []
    expire_count = 0
    expire_status = 302

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.requests.append(('POST', self.path))
        form = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        ok = self.path == '/login' and self.login_ok and form == {'username': ['operator'], 'password': ['secret']}
        self.send_response(302 if ok else 200)
        if ok:
            self.send_header('Location', '/statistics')
            self.send_header('Set-Cookie', 'session=test; Path=/; HttpOnly')
        self.end_headers()

    def do_GET(self):
        self.requests.append(('GET', self.path))
        ok = self.path == '/api/statistics' and self.headers.get('Cookie') == 'session=test'
        if type(self).expire_count:
            type(self).expire_count -= 1
            self.send_response(self.expire_status)
            self.send_header('Location', '/login?next=/api/statistics')
            self.end_headers()
            return
        self.send_response(self.status if ok else 401)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(self.payload).encode())


class MCPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel = ThreadingHTTPServer(('127.0.0.1', 0), FakePanel)
        cls.thread = threading.Thread(target=cls.panel.serve_forever, daemon=True)
        cls.thread.start()
        cls.env = {'LEADPROGRESS_URL': f'http://127.0.0.1:{cls.panel.server_port}',
                   'LEADPROGRESS_USERNAME': 'operator', 'LEADPROGRESS_PASSWORD': 'secret'}

    @classmethod
    def tearDownClass(cls):
        cls.panel.shutdown()
        cls.panel.server_close()
        cls.thread.join()

    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        FakePanel.expire_count = 0
        FakePanel.expire_status = 302
        FakePanel.status = 200
        FakePanel.login_ok = True
        FakePanel.payload = {'Klient A': {'client_id': 12, 'sent': 100}, 'Klient B': {'client_id': 7}}
        for row in FakePanel.payload.values():
            row.update(in_progress=True, is_active=True, is_paused=False)
        FakePanel.requests = []
        self.environment = patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_stdio_initialize_list_and_call(self):
        async def run():
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(server.__file__).resolve())], env=self.env, cwd=str(Path.home()))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {'get_reply_prompts', 'attach_draft_attachment', 'detach_draft_attachment', 'get_client_ai_context', 'list_clients', 'list_client_attachments', 'list_conversations', 'get_conversation', 'read_attachment', 'list_draft_attachments', 'create_reply_draft', 'update_reply_draft', 'send_reply_draft', 'get_clients_summary', 'get_conversations_summary', 'list_salesmen', 'list_calendar_events', 'list_salesman_absences', 'get_calendar_summary', 'get_salesman_availability', 'create_calendar_event', 'reschedule_calendar_event', 'record_call_result'})
                    send_tool = next(tool for tool in tools.tools if tool.name == 'send_reply_draft')
                    self.assertEqual(set(send_tool.inputSchema['required']), {'client_id', 'email', 'draft_id'})
                    result = await session.call_tool('list_clients', {})
                    self.assertFalse(result.isError)
                    self.assertEqual([{k: row[k] for k in ('client_id', 'username')}
                                      for row in json.loads(result.content[0].text)['items']],
                        [{'client_id': 12, 'username': 'Klient A'}, {'client_id': 7, 'username': 'Klient B'}])
        asyncio.run(asyncio.wait_for(run(), 30))
        self.assertEqual(FakePanel.requests, [('POST', '/login'), ('GET', '/api/statistics')])

    def test_reuses_login_across_calls(self):
        server.list_clients()
        client = server.login()
        server.get_json('/api/statistics')
        self.assertIs(server.login(), client)
        self.assertEqual(FakePanel.requests,
                         [('POST', '/login'), ('GET', '/api/statistics'), ('GET', '/api/statistics')])

    def test_reauthenticates_on_expired_session(self):
        for status in (302, 401):
            with self.subTest(status=status):
                server.close_session()
                FakePanel.requests = []
                server.list_clients()
                FakePanel.expire_count = 1
                FakePanel.expire_status = status
                self.assertEqual(len(server.list_clients()['items']), 2)
                self.assertEqual(FakePanel.requests.count(('POST', '/login')), 2)
                self.assertEqual(FakePanel.requests.count(('GET', '/api/statistics')), 3)

    def test_stops_after_one_retry(self):
        FakePanel.expire_count = 10
        with self.assertRaisesRegex(ToolError, 'Session rejected'):
            server.list_clients()
        self.assertEqual(FakePanel.requests.count(('POST', '/login')), 2)
        self.assertIsNone(server._client)

    def test_client_filters_and_safe_fields(self):
        base = {'client_id': 1, 'in_progress': True, 'is_active': True, 'is_paused': False}
        FakePanel.payload = {
            'Running': {**base, 'description': 'Printing', 'imap_pass': 'never return this'},
            'Paused': {**base, 'is_paused': True},
            'Inactive': {**base, 'is_active': False},
            'Stopped': {**base, 'in_progress': False},
        }
        result = server.list_clients()['items']
        self.assertEqual([row['username'] for row in result], ['Running'])
        self.assertEqual(result[0]['description'], 'Printing')
        self.assertNotIn('imap_pass', result[0])

    def test_failed_login_can_be_retried(self):
        FakePanel.login_ok = False
        with self.assertRaises(ToolError):
            server.list_clients()
        self.assertIsNone(server._client)
        FakePanel.login_ok = True
        self.assertEqual(len(server.list_clients()['items']), 2)

    def test_bad_login_stops_before_statistics(self):
        FakePanel.login_ok = False
        with self.assertRaisesRegex(ToolError, 'Login failed'):
            server.list_clients()
        self.assertEqual(FakePanel.requests, [('POST', '/login')])

    def test_forbidden(self):
        FakePanel.status = 403
        with self.assertRaisesRegex(ToolError, 'Access denied'):
            server.list_clients()
        self.assertEqual(FakePanel.requests.count(('POST', '/login')), 1)

    def test_empty_statistics(self):
        FakePanel.payload = {}
        self.assertEqual(server.list_clients()['items'], [])

    def test_invalid_statistics(self):
        FakePanel.payload = {'error': 'failure'}
        with self.assertRaisesRegex(ToolError, 'Unexpected'):
            server.list_clients()

    def test_missing_credentials(self):
        with patch.dict(os.environ, {'LEADPROGRESS_PASSWORD': ''}):
            with self.assertRaisesRegex(ToolError, 'Set LEADPROGRESS'):
                server.list_clients()
        self.assertEqual(FakePanel.requests, [])


if __name__ == '__main__':
    unittest.main()
