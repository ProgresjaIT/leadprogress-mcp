import asyncio
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import server
from tests.test_attachments import AttachmentPanel, office_file, pdf_file
from tests.test_server import ThreadingHTTPServer


def conversation():
    return {'status': 'ok', 'mailbox_user_id': 394, 'client_id': 105, 'active_client_id': 105,
            'selected_email': 'test@example.com', 'selected_thread_id': 42,
            'timeline': [{'payload': {'attachments': [{'id': 99, 'filename': 'history.pdf'}]}}],
            'pending_approvals': [{'id': 7, 'email_address': 'test@example.com'}],
            'pending_questions': [], 'threads_for_selected_email': []}


class DraftAttachmentTests(unittest.TestCase):
    def test_listing_preserves_selection_and_empty_list(self):
        with patch('server._get_conversation', return_value=conversation()) as chat, \
                patch('server.get_json', return_value={'status': 'ok', 'attachments': []}) as api:
            result = server.list_draft_attachments(394, 'test@example.com', 7, 105, 42)
            self.assertEqual(result['attachments'], [])
            self.assertEqual(result['client_id'], 105)
            chat.assert_called_once_with(394, 'test@example.com', thread_id=42, conversation_client_id=105)
            api.assert_called_once_with('/proxy/drafts/7/attachments')

    def test_foreign_missing_or_ambiguous_draft_never_fetches_files(self):
        for drafts in ([], [{'id': 8, 'email_address': 'test@example.com'}],
                       [{'id': 7, 'email_address': 'other@example.com'}],
                       [{'id': 7, 'email_address': 'test@example.com'}] * 2):
            data = conversation()
            data['pending_approvals'] = drafts
            with self.subTest(drafts=drafts), patch('server._get_conversation', return_value=data), \
                    patch('server.get_json') as api, patch('server._download_attachment') as download:
                with self.assertRaises(ToolError):
                    server.list_draft_attachments(394, 'test@example.com', 7)
                with self.assertRaises(ToolError):
                    server.read_attachment(394, 'test@example.com', 1, draft_id=7)
                api.assert_not_called()
                download.assert_not_called()

    def test_invalid_id_rejected_before_network(self):
        with patch('server._get_conversation') as chat:
            for draft_id in (0, -1):
                with self.assertRaises(ToolError):
                    server.list_draft_attachments(394, 'test@example.com', draft_id)
                with self.assertRaises(ToolError):
                    server.read_attachment(394, 'test@example.com', 1, draft_id=draft_id)
            chat.assert_not_called()

    def test_bad_metadata_rejected(self):
        item = {'id': 1, 'filename': 'file.pdf'}
        for response in ([], {'status': 'error'}, {'status': 'ok'},
                         {'status': 'ok', 'attachments': [None]},
                         {'status': 'ok', 'attachments': [{'id': True, 'filename': 'x.pdf'}]},
                         {'status': 'ok', 'attachments': [item, item]}):
            with self.subTest(response=response), patch('server._get_conversation', return_value=conversation()), \
                    patch('server.get_json', return_value=response), patch('server._download_attachment') as download:
                with self.assertRaises(ToolError):
                    server.read_attachment(394, 'test@example.com', 1, draft_id=7)
                download.assert_not_called()

    def test_history_file_cannot_be_read_as_draft_file(self):
        with patch('server._get_conversation', return_value=conversation()), \
                patch('server.get_json', return_value={'status': 'ok', 'attachments': []}), \
                patch('server._download_attachment') as download:
            with self.assertRaises(ToolError):
                server.read_attachment(394, 'test@example.com', 99, draft_id=7)
            download.assert_not_called()

    def test_limits_and_unsupported_files_before_download(self):
        for item in ({'id': 1, 'filename': 'x.exe'},
                     {'id': 1, 'filename': 'x.pdf', 'size': server.MAX_FILE_BYTES + 1}):
            with patch('server._get_conversation', return_value=conversation()), \
                    patch('server.get_json', return_value={'status': 'ok', 'attachments': [item]}), \
                    patch('server._download_attachment') as download:
                with self.assertRaises(ToolError):
                    server.read_attachment(394, 'test@example.com', 1, draft_id=7)
                download.assert_not_called()

    def test_removed_file_download_error_is_reported(self):
        with patch('server._get_conversation', return_value=conversation()), \
                patch('server.get_json', return_value={'status': 'ok', 'attachments': [{'id': 1, 'filename': 'x.pdf'}]}), \
                patch('server._download_attachment', side_effect=ToolError('Attachment download returned HTTP 404.')):
            with self.assertRaisesRegex(ToolError, '404'):
                server.read_attachment(394, 'test@example.com', 1, draft_id=7)


class DraftPanel(AttachmentPanel):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path not in ('/crm/partial/chat', '/proxy/drafts/7/attachments'):
            return super().do_GET()
        self.requests.append(('GET', self.path))
        if self.headers.get('Cookie') != 'session=test':
            self.send_response(401)
            self.end_headers()
            return
        data = conversation() if path == '/crm/partial/chat' else {
            'status': 'ok', 'attachments': [
                {'id': key, 'filename': name, 'size': len(body), 'position': key}
                for key, (name, body) in self.files.items()]}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


class DraftAttachmentMCPTest(unittest.TestCase):
    def test_list_and_read_three_formats_through_stdio(self):
        DraftPanel.files = {1: ('test.odt', office_file('odt')), 2: ('test.docx', office_file('docx')),
                            3: ('test.pdf', pdf_file())}
        DraftPanel.requests = []
        panel = ThreadingHTTPServer(('127.0.0.1', 0), DraftPanel)
        thread = threading.Thread(target=panel.serve_forever, daemon=True)
        thread.start()

        async def run():
            params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__).resolve())],
                env={'LEADPROGRESS_URL': f'http://127.0.0.1:{panel.server_port}',
                     'LEADPROGRESS_USERNAME': 'operator', 'LEADPROGRESS_PASSWORD': 'secret'})
            selection = {'client_id': 394, 'email': 'test@example.com', 'conversation_client_id': 105,
                         'thread_id': 42, 'draft_id': 7}
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool('list_draft_attachments', selection)
                    self.assertFalse(result.isError, str(result.content))
                    data = result.structuredContent or json.loads(result.content[0].text)
                    self.assertEqual([a['id'] for a in data['attachments']], [1, 2, 3])
                    for key, kind in ((1, 'odt'), (2, 'docx'), (3, 'pdf')):
                        result = await client.call_tool('read_attachment', {**selection, 'attachment_id': key})
                        self.assertFalse(result.isError, str(result.content))
                        data = result.structuredContent or json.loads(result.content[0].text)
                        self.assertEqual(data['draft_id'], 7)
                        self.assertEqual(data['format'], kind)
                        self.assertIn('PDF attachment text' if kind == 'pdf' else 'Zażółć', data['text'])
                    result = await client.call_tool('read_attachment', {**selection, 'attachment_id': 99})
                    self.assertTrue(result.isError)
        try:
            asyncio.run(asyncio.wait_for(run(), 45))
            self.assertEqual([p for method, p in DraftPanel.requests if '/download' in p],
                             [f'/proxy/attachment/{i}/download' for i in (1, 2, 3)])
            self.assertEqual([p for method, p in DraftPanel.requests if method == 'POST'], ['/login'])
        finally:
            panel.shutdown()
            panel.server_close()
            thread.join()
