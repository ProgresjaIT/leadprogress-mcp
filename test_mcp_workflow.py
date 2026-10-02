"""Exercise all public tools through stdio MCP, with HTTP replaced in the child process."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


RUNNER = r'''
import io
import json
import zipfile
import httpx
import server

drafts = []
sent = []
buffer = io.BytesIO()
with zipfile.ZipFile(buffer, 'w') as archive:
    archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Attachment text</w:t></w:r></w:p></w:body></w:document>')
timeline = [{'type': 'message', 'payload': {'id': 1, 'attachments': [{'id': 7, 'filename': 'sample.docx'}]}}]
timeline += [{'type': 'event', 'payload': {'id': i}} for i in range(2, 32)]

def handle(request):
    path = request.url.path
    payload = json.loads(request.content) if request.content else None
    if request.method == 'GET':
        if path == '/api/statistics':
            return httpx.Response(200, json={'Mailbox': {'client_id': 27, 'is_active': True, 'in_progress': True, 'pending_questions': 3}})
        if path == '/crm':
            rows = [{'client_id': 77, 'email_address': f'a{i}@example.com', 'bucket': 'active', 'stage': 'lead'} for i in range(30)]
            rows.append({'client_id': 77, 'email_address': 'z@example.com', 'bucket': 'active', 'stage': 'lead'})
            return httpx.Response(200, json={'status': 'ok', 'mailbox_user_id': 27, 'conversations_by_email': rows})
        if path == '/crm/partial/chat':
            assert request.url.params['conversation_client_id'] == '77'
            return httpx.Response(200, json={'status': 'ok', 'mailbox_user_id': 27, 'client_id': 77,
                'active_client_id': 77, 'selected_email': 'z@example.com', 'selected_thread_id': None,
                'timeline': timeline + sent, 'threads_for_selected_email': [],
                'pending_approvals': drafts, 'pending_questions': []})
        if path == '/proxy/attachment/7/download':
            return httpx.Response(200, content=buffer.getvalue())
    if request.method == 'POST':
        if path == '/proxy/create_draft':
            assert payload['client_id'] == 77 and payload['sales_user_id'] == 27
            assert not drafts
            drafts.append({'id': 81, 'email_address': 'z@example.com', 'subject': '', 'body': ''})
            return httpx.Response(200, json={'status': 'ok', 'draft_id': 81})
        if path == '/proxy/save_message/81':
            drafts[0].update(payload)
            return httpx.Response(200, json={'status': 'saved'})
        if path == '/proxy/approve_message/81':
            assert payload == {'client_id': 77}
            assert drafts[0]['subject'] == 'Offer' and drafts[0]['body'] == 'Hello.'
            sent.append({'type': 'message', 'payload': drafts.pop()})
            return httpx.Response(200, json={'status': 'sent'})
    raise AssertionError(f'Unexpected request: {request.method} {path}')

server._client = httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handle))
server.mcp.run(transport='stdio')
'''


class MCPWorkflowTests(unittest.TestCase):
    def test_all_nine_tools_and_draft_lifecycle(self):
        async def run(script):
            params = StdioServerParameters(command=sys.executable, args=[str(script)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    called = set()

                    async def call(name, **args):
                        result = await session.call_tool(name, args)
                        self.assertFalse(result.isError, result.content)
                        called.add(name)
                        return json.loads(result.content[0].text)

                    clients = await call('list_clients')
                    self.assertEqual(clients['items'][0]['client_id'], 27)
                    summary = await call('get_clients_summary')
                    self.assertEqual(summary['total_clients'], clients['total'])
                    contacts = await call('list_conversations', client_id=27)
                    self.assertEqual(contacts['returned'], 25)
                    contacts = await call('list_conversations', client_id=27, offset=contacts['next_offset'])
                    row = contacts['items'][-1]
                    args = {'client_id': 27, 'email': row['email_address'], 'conversation_client_id': row['client_id']}
                    summary = await call('get_conversations_summary', client_id=27)
                    self.assertEqual(summary['total_rows'], 31)
                    chat = await call('get_conversation', **args)
                    self.assertEqual(chat['timeline_page']['returned'], 20)
                    older = await call('get_conversation', **args, before=chat['timeline_page']['next_before'])
                    attachment = older['timeline'][0]['payload']['attachments'][0]
                    result = await call('read_attachment', **args, attachment_id=attachment['id'])
                    self.assertIn('Attachment text', result['text'])
                    created = await call('create_reply_draft', **args, subject='Offer', body='Hello')
                    chat = await call('get_conversation', **args)
                    draft = chat['pending_approvals'][0]
                    self.assertEqual(created['draft_id'], draft['id'])
                    updated = await call('update_reply_draft', **args, draft_id=draft['id'], body=draft['body'] + '.')
                    self.assertEqual(updated['subject'], draft['subject'])
                    sent = await call('send_reply_draft', **args, draft_id=draft['id'])
                    self.assertTrue(sent['sent'])
                    chat = await call('get_conversation', **args)
                    self.assertEqual(chat['pending_approvals'], [])
                    self.assertEqual(chat['timeline'][-1]['payload']['body'], 'Hello.')
                    self.assertEqual(len(called), 9)

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'runner.py'
            script.write_text('import sys\nsys.path.insert(0, ' + repr(str(Path(__file__).parent)) + ')\n' + RUNNER, encoding='utf-8')
            asyncio.run(asyncio.wait_for(run(script), 45))


if __name__ == '__main__':
    unittest.main()
