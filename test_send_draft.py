import json
import unittest

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import server


class SendDraftTests(unittest.TestCase):
    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        self.calls = []
        self.owner = 27
        self.selected_thread = None
        self.drafts = [{'id': 81, 'email_address': 'test@example.com',
                        'subject': 'Subject', 'body': 'Saved body'}]
        self.response = httpx.Response(200, json={'status': 'sent'})
        self.failure = None
        server._client = httpx.Client(base_url='https://panel.example',
                                      transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        payload = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, payload))
        if request.method == 'GET':
            self.assertEqual(request.url.path, '/crm/partial/chat')
            return httpx.Response(200, json={
                'status': 'ok', 'mailbox_user_id': 27, 'client_id': self.owner,
                'active_client_id': self.owner, 'selected_email': 'test@example.com',
                'selected_thread_id': self.selected_thread,
                'timeline': [], 'threads_for_selected_email': [],
                'pending_approvals': self.drafts, 'pending_questions': []})
        self.assertEqual(request.url.path, '/proxy/approve_message/81')
        if self.failure:
            raise self.failure
        return self.response

    def send(self, **kwargs):
        return server.send_reply_draft(**({'client_id': 27, 'email': 'test@example.com',
                                          'draft_id': 81} | kwargs))

    def writes(self):
        return [call for call in self.calls if call[0] == 'POST']

    def test_sends_once_without_overwriting_saved_content(self):
        result = self.send()
        self.assertEqual(self.calls, [('GET', '/crm/partial/chat', None),
                                     ('POST', '/proxy/approve_message/81', {'client_id': 27})])
        self.assertEqual(result['status'], 'sent')
        self.assertTrue(result['sent'])
        self.assertEqual(result['draft_id'], 81)

    def test_sales_mailbox_uses_conversation_owner(self):
        self.owner = 99
        self.selected_thread = 5
        result = self.send(conversation_client_id=99, thread_id=5)
        self.assertEqual(self.writes()[0][2], {'client_id': 99})
        self.assertEqual(result['mailbox_user_id'], 27)
        self.assertEqual(result['client_id'], 99)

    def test_missing_foreign_or_ambiguous_draft_never_sent(self):
        original = self.drafts[0]
        for drafts in ([], [dict(original, email_address='other@example.com')],
                       [dict(original, id=82)], [original, original]):
            self.drafts = drafts
            with self.assertRaisesRegex(ToolError, 'Draft is not present'):
                self.send()
        self.assertEqual(self.writes(), [])

    def test_wrong_owner_or_thread_never_sent(self):
        with self.assertRaisesRegex(ToolError, 'requested thread'):
            self.send(thread_id=5)
        self.owner = 99
        with self.assertRaisesRegex(ToolError, 'requested conversation'):
            self.send()
        self.assertEqual(self.writes(), [])

    def test_empty_or_invalid_content_never_sent(self):
        for field in ('subject', 'body'):
            for value in ('', ' ', None, 123):
                self.drafts = [{'id': 81, 'email_address': 'test@example.com',
                                'subject': 'Subject', 'body': 'Body', field: value}]
                with self.subTest(field=field, value=value), self.assertRaises(ToolError):
                    self.send()
        self.assertEqual(self.writes(), [])

    def test_invalid_input_never_calls_api(self):
        for args in ({'draft_id': 0}, {'client_id': 0}, {'email': ''},
                     {'conversation_client_id': -1}, {'thread_id': -1}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.send(**args)
        self.assertEqual(self.calls, [])

    def test_timeout_never_retries(self):
        self.failure = httpx.ReadTimeout('test')
        with self.assertRaisesRegex(ToolError, 'draft 81.*not confirmed'):
            self.send()
        self.assertEqual(len(self.writes()), 1)

    def test_unconfirmed_responses_never_report_success_or_retry(self):
        for response in (httpx.Response(403), httpx.Response(404), httpx.Response(500),
                         httpx.Response(200, text='bad JSON'),
                         httpx.Response(200, json={'status': 'error'}),
                         httpx.Response(200, json={'status': 'saved'})):
            self.calls = []
            self.response = response
            with self.subTest(response=response), self.assertRaisesRegex(ToolError, 'may already have been sent'):
                self.send()
            self.assertEqual(len(self.writes()), 1)

    def test_expired_session_never_replays_send(self):
        self.response = httpx.Response(302, headers={'Location': '/login'})
        with self.assertRaisesRegex(ToolError, 'not confirmed'):
            self.send()
        self.assertEqual(len(self.writes()), 1)
        self.assertIsNone(server._client)


if __name__ == '__main__':
    unittest.main()
