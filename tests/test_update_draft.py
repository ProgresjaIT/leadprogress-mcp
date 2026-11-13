import json
import unittest

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import server


class UpdateDraftTests(unittest.TestCase):
    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        self.writes = []
        self.failure = False
        self.owner = 27
        self.drafts = [{'id': 81, 'email_address': 'test@example.com',
                        'subject': 'Original subject', 'body': 'Original body'}]
        server._client = httpx.Client(base_url='https://panel.example',
                                      transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        if request.method == 'GET':
            self.assertEqual(request.url.path, '/crm/partial/chat')
            return httpx.Response(200, json={
                'status': 'ok', 'mailbox_user_id': 27, 'client_id': self.owner,
                'active_client_id': self.owner, 'selected_email': 'test@example.com',
                'timeline': [], 'threads_for_selected_email': [],
                'pending_approvals': self.drafts, 'pending_questions': []})
        self.assertEqual(request.url.path, '/proxy/save_message/81')
        self.writes.append(json.loads(request.content))
        if self.failure:
            raise httpx.ReadTimeout('test')
        return httpx.Response(200, json={'status': 'saved'})

    def update(self, **kwargs):
        return server.update_reply_draft(**({'client_id': 27, 'email': 'test@example.com',
                                            'draft_id': 81} | kwargs))

    def test_body_update_preserves_subject(self):
        result = self.update(body='Original body.')
        self.assertEqual(self.writes, [{'subject': 'Original subject', 'body': 'Original body.'}])
        self.assertFalse(result['sent'])
        self.assertEqual(result['draft_id'], 81)

    def test_subject_update_preserves_body(self):
        self.update(subject='New subject')
        self.assertEqual(self.writes, [{'subject': 'New subject', 'body': 'Original body'}])

    def test_both_fields_and_different_owner(self):
        self.owner = 99
        result = self.update(subject='New', body='Text', conversation_client_id=99)
        self.assertEqual(result['client_id'], 99)
        self.assertEqual(self.writes, [{'subject': 'New', 'body': 'Text'}])

    def test_missing_or_foreign_draft_never_written(self):
        for drafts in ([], [{'id': 81, 'email_address': 'other@example.com'}]):
            self.drafts = drafts
            with self.assertRaisesRegex(ToolError, 'Draft is not present'):
                self.update(body='Text')
        self.assertEqual(self.writes, [])

    def test_wrong_owner_never_written(self):
        self.owner = 99
        with self.assertRaisesRegex(ToolError, 'requested conversation'):
            self.update(body='Text')
        self.assertEqual(self.writes, [])

    def test_invalid_input_never_written(self):
        for args in ({}, {'body': ''}, {'subject': 'Bad\nsubject'}, {'body': 'Text', 'draft_id': 0}):
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.update(**args)
        self.assertEqual(self.writes, [])

    def test_timeout_never_retries(self):
        self.failure = True
        with self.assertRaisesRegex(ToolError, 'outcome unknown'):
            self.update(body='Text')
        self.assertEqual(len(self.writes), 1)


if __name__ == '__main__':
    unittest.main()
