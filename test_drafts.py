import json
import unittest
from unittest.mock import patch

import httpx
from mcp.server.fastmcp.exceptions import ToolError
import server


class CreateDraftTests(unittest.TestCase):
    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        self.calls = []
        self.owner = 394
        self.rows = [{'client_id':394, 'email_address':'test@example.com'}]
        self.create_response = {'status':'ok', 'draft_id':81}
        self.save_status = 200
        self.save_response = {'status':'saved'}
        self.create_failure = None
        self.save_failure = None
        self.hidden_thread = False
        server._client = httpx.Client(base_url='https://panel.example',
                                      transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method,path,body))
        if path == '/crm':
            return httpx.Response(200,json={'status':'ok','mailbox_user_id':394,
                                           'conversations_by_email':self.rows})
        if path == '/crm/partial/chat':
            thread = request.url.params.get('thread_id')
            return httpx.Response(200,json={'status':'ok','mailbox_user_id':394,
                'client_id':self.owner,'active_client_id':self.owner,'selected_email':'test@example.com',
                'selected_thread_id':None if self.hidden_thread or thread is None else int(thread),
                'timeline':[],'threads_for_selected_email':[],'pending_approvals':[],'pending_questions':[]})
        if path == '/proxy/create_draft':
            if self.create_failure:
                raise self.create_failure
            return httpx.Response(200,json=self.create_response)
        if path == '/proxy/save_message/81':
            if self.save_failure:
                raise self.save_failure
            return httpx.Response(self.save_status,json=self.save_response)
        self.fail('Unexpected endpoint: ' + path)

    def create(self, **kwargs):
        return server.create_reply_draft(**({'client_id':394,'email':'test@example.com',
                                            'subject':' Offer ', 'body':' Message\nSecond line '} | kwargs))

    def writes(self):
        return [call for call in self.calls if call[0]=='POST']

    def test_create_and_save_without_sending(self):
        result = self.create()
        self.assertEqual(result['draft_id'],81)
        self.assertTrue(result['new_thread_created'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.writes(), [
            ('POST','/proxy/create_draft',{'client_id':394,'sales_user_id':394,
                                         'email_address':'test@example.com','thread_id':None}),
            ('POST','/proxy/save_message/81',{'subject':'Offer','body':'Message\nSecond line'})])
        self.assertEqual([method for method,path,body in self.calls],['GET','GET','POST','POST'])

    def test_sales_mailbox_and_existing_thread(self):
        self.owner=77
        self.rows=[{'client_id':77,'email_address':'test@example.com'}]
        result = self.create(conversation_client_id=77,thread_id=9)
        self.assertFalse(result['new_thread_created'])
        self.assertEqual(result['client_id'],77)
        self.assertEqual(self.writes()[0][2]['thread_id'],9)
        self.assertEqual(self.writes()[0][2]['sales_user_id'],394)

    def test_denied_contact_never_written(self):
        self.rows=[]
        with self.assertRaisesRegex(ToolError,'Contact is not present'):
            self.create()
        self.assertEqual(self.writes(),[])

    def test_contact_beyond_first_page_can_create_draft(self):
        self.rows = [{'client_id': 394, 'email_address': f'a{i}@example.com'}
                     for i in range(30)] + self.rows
        result = self.create()
        self.assertEqual(result['draft_id'], 81)
        self.assertEqual(len(self.writes()), 2)

    def test_invalid_thread_never_written(self):
        self.hidden_thread=True
        with self.assertRaisesRegex(ToolError,'requested thread'):
            self.create(thread_id=9)
        self.assertEqual(self.writes(),[])

    def test_invalid_input_never_calls_api(self):
        for kwargs in ({'subject':' '},{'body':''},{'thread_id':0},{'client_id':0},
                       {'subject':'Title\nInjected'}, {'email':'x\ry@example.com'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ToolError):
                self.create(**kwargs)
        self.assertEqual(self.calls,[])

    def test_create_timeout_not_retried(self):
        self.create_failure=httpx.ReadTimeout('test')
        with self.assertRaisesRegex(ToolError,'outcome unknown'):
            self.create()
        self.assertEqual(len(self.writes()),1)

    def test_partial_failure_preserves_draft_id(self):
        for status in (403,500,302):
            with self.subTest(status=status):
                # A 302 invalidates the shared session; place it last.
                self.calls=[]
                self.save_status=status
                with self.assertRaisesRegex(ToolError,'Draft 81 was created'):
                    self.create()
                self.assertEqual(len(self.writes()),2)

    def test_save_timeout_not_retried(self):
        self.save_failure=httpx.ReadTimeout('test')
        with self.assertRaisesRegex(ToolError,'Draft 81 was created'):
            self.create()
        self.assertEqual(len(self.writes()),2)

    def test_missing_draft_id_stops_before_save(self):
        self.create_response={'status':'ok'}
        with self.assertRaisesRegex(ToolError,'no valid ID'):
            self.create()
        self.assertEqual(len(self.writes()),1)

    def test_wrong_save_status_not_success(self):
        self.save_response={'status':'error'}
        with self.assertRaisesRegex(ToolError,'Draft 81 was created'):
            self.create()


if __name__ == '__main__':
    unittest.main()
