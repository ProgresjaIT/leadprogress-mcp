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
from tests.test_draft_attachments import conversation


class DraftFileTests(unittest.TestCase):
    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        self.chat = conversation()
        self.files = [{'id': 1, 'filename': 'existing.pdf', 'position': 1}]
        self.library = [{'id': 2, 'filename': 'offer.pdf', 'type': 'out_going_to_client'}]
        self.calls = []
        self.failure = None
        self.response = None
        self.selection = dict(client_id=394, email='test@example.com', draft_id=7,
                              conversation_client_id=105, thread_id=42)
        server._client = httpx.Client(base_url='https://panel.example',
                                      transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        path = request.url.path
        payload = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, payload))
        if request.method == 'GET':
            if path == '/crm/partial/chat':
                return httpx.Response(200, json=self.chat)
            if path == '/proxy/drafts/7/attachments':
                return httpx.Response(200, json={'status': 'ok', 'attachments': self.files})
            if path == '/proxy/clients/105/available_attachments':
                self.assertEqual(request.url.params['type'], 'out_going_to_client')
                return httpx.Response(200, json={'status': 'ok', 'attachments': self.library})
            raise AssertionError(f'Unexpected read: {path}')
        if self.failure:
            raise self.failure
        if self.response is not None:
            return self.response
        if request.method == 'POST':
            self.assertEqual(path, '/proxy/drafts/7/attachments')
            self.assertEqual(payload, {'attachment_ids': [2]})
            self.files.append({'id': 2, 'filename': 'offer.pdf', 'position': 2})
            status = 'linked'
        else:
            self.assertEqual(request.method, 'DELETE')
            self.assertEqual(path, '/proxy/drafts/7/attachments/1')
            self.assertIsNone(payload)
            self.files = [f for f in self.files if f['id'] != 1]
            status = 'deleted'
        return httpx.Response(200, json={'status': status, 'attachments': self.files})

    def writes(self):
        return [call for call in self.calls if call[0] != 'GET']

    def attach(self, **kwargs):
        return server.attach_draft_attachment(**(self.selection | {'attachment_id': 2} | kwargs))

    def detach(self, **kwargs):
        return server.detach_draft_attachment(**(self.selection | {'attachment_id': 1} | kwargs))

    def test_tools_exist(self):
        for name in ('attach_draft_attachment', 'detach_draft_attachment'):
            self.assertTrue(callable(getattr(server, name, None)), name)

    def test_attach_uses_owner_library_and_preserves_existing_files(self):
        result = self.attach()
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'attached')
        self.assertEqual(result['client_id'], 105)
        self.assertEqual(result['mailbox_user_id'], 394)
        self.assertEqual([f['id'] for f in result['attachments']], [1, 2])
        self.assertEqual(self.writes(), [('POST', '/proxy/drafts/7/attachments', {'attachment_ids': [2]})])

    def test_detach_only_unlinks_and_preserves_other_files(self):
        self.files.append({'id': 2, 'filename': 'offer.pdf', 'position': 2})
        result = self.detach()
        self.assertEqual(result['status'], 'detached')
        self.assertTrue(result['changed'])
        self.assertEqual([f['id'] for f in result['attachments']], [2])
        self.assertEqual(self.writes(), [('DELETE', '/proxy/drafts/7/attachments/1', None)])

    def test_already_attached_or_absent_is_noop(self):
        self.files.append({'id': 2, 'filename': 'offer.pdf'})
        self.assertFalse(self.attach()['changed'])
        self.assertFalse(self.detach(attachment_id=3)['changed'])
        self.assertEqual(self.writes(), [])

    def test_invalid_ids_before_network(self):
        for method in (self.attach, self.detach):
            for key in ('client_id', 'draft_id', 'attachment_id', 'conversation_client_id'):
                for value in (0, -1, True):
                    with self.subTest(key=key, value=value), self.assertRaises(ToolError):
                        method(**{key: value})
        self.assertEqual(self.calls, [])

    def test_foreign_missing_or_ambiguous_draft_never_writes(self):
        for drafts in ([], [{'id': 7, 'email_address': 'other@example.com'}],
                       [{'id': 7, 'email_address': 'test@example.com'}] * 2):
            self.chat['pending_approvals'] = drafts
            for method in (self.attach, self.detach):
                with self.subTest(drafts=drafts), self.assertRaises(ToolError):
                    method()
        self.assertEqual(self.writes(), [])

    def test_wrong_thread_or_owner_never_writes(self):
        for kwargs in ({'thread_id': 99}, {'conversation_client_id': 106}):
            for method in (self.attach, self.detach):
                with self.subTest(kwargs=kwargs), self.assertRaises(ToolError):
                    method(**kwargs)
        self.assertEqual(self.writes(), [])

    def test_missing_or_incoming_library_file_never_attaches(self):
        for library in ([], [{'id': 2, 'filename': 'x.pdf', 'type': 'incoming'}]):
            self.library = library
            with self.assertRaises(ToolError):
                self.attach()
        self.assertEqual(self.writes(), [])

    def test_attachment_beyond_first_page(self):
        self.library = [{'id': i, 'filename': 'x.pdf', 'type': 'out_going_to_client'} for i in range(100, 210)] + self.library
        self.assertTrue(self.attach()['changed'])

    def test_timeout_is_never_retried(self):
        for method in (self.attach, self.detach):
            self.calls.clear()
            self.failure = httpx.ReadTimeout('timeout')
            with self.assertRaisesRegex(ToolError, 'not repeated'):
                method()
            self.assertEqual(len(self.writes()), 1)

    def test_expired_session_is_not_retried(self):
        for method in (self.attach, self.detach):
            self.calls.clear()
            server._client = httpx.Client(base_url='https://panel.example',
                                          transport=httpx.MockTransport(self.handle))
            self.response = httpx.Response(401)
            with self.assertRaisesRegex(ToolError, 'not repeated'):
                method()
            self.assertEqual(len(self.writes()), 1)
            self.assertIsNone(server._client)

    def test_bad_write_metadata_is_not_accepted(self):
        for files in (None, [None], [{'id': True, 'filename': 'x.pdf'}],
                      [{'id': 1, 'filename': 'x.pdf'}] * 2):
            self.calls.clear()
            self.response = httpx.Response(200, json={'status': 'linked', 'attachments': files})
            with self.assertRaisesRegex(ToolError, 'not confirmed'):
                self.attach()
            self.assertEqual(len(self.writes()), 1)

    def test_errors_or_unconfirmed_changes_never_report_success(self):
        for method, status, files in ((self.attach, 'linked', [{'id': 2, 'filename': 'x.pdf'}]),
                                     (self.attach, 'linked', self.files),
                                     (self.detach, 'deleted', self.files)):
            for response in (httpx.Response(403), httpx.Response(500),
                             httpx.Response(200, text='not json'),
                             httpx.Response(200, json={'status': 'error'}),
                             httpx.Response(200, json={'status': status, 'attachments': files})):
                self.calls.clear()
                self.response = response
                with self.subTest(status=status, response=response), self.assertRaises(ToolError):
                    method()
                self.assertEqual(len(self.writes()), 1)


class DraftFileMCPTests(unittest.TestCase):
    def test_attach_and_detach_through_stdio(self):
        async def run():
            script = ('import sys; sys.path.insert(0, ' + repr(str(Path(server.__file__).parent)) + '); '
                      'from tests.test_manage_draft_attachments import DraftFileTests; '
                      'import server; fixture = DraftFileTests(); fixture.setUp(); server.mcp.run()')
            params = StdioServerParameters(command=sys.executable, args=['-B', '-c', script])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    args = dict(client_id=394, email='test@example.com', draft_id=7,
                                conversation_client_id=105, thread_id=42)
                    for name, aid, ids in (('attach_draft_attachment', 2, [1, 2]),
                                           ('detach_draft_attachment', 1, [2])):
                        result = await session.call_tool(name, {**args, 'attachment_id': aid})
                        self.assertFalse(result.isError, str(result.content))
                        data = result.structuredContent or json.loads(result.content[0].text)
                        self.assertEqual([f['id'] for f in data['attachments']], ids)
        asyncio.run(asyncio.wait_for(run(), 30))
