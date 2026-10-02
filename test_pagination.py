import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
import server


class PaginationTests(unittest.TestCase):
    def test_clients_filter_before_page_and_summary(self):
        data = {f'Client {i:02}': {'client_id': i + 1, 'is_active': True,
                'in_progress': True, 'salesman': 'Anna' if i < 30 else 'Jan'} for i in range(40)}
        with patch.object(server, 'get_json', return_value=data):
            first = server.list_clients(query='CLIENT', salesman='anna')
            second = server.list_clients(query='CLIENT', salesman='anna', offset=first['next_offset'])
            summary = server.get_clients_summary(query='CLIENT', salesman='anna')
            empty = server.list_clients(offset=100)
        self.assertEqual(first['total'], 30)
        self.assertEqual(first['returned'], 25)
        self.assertEqual(second['returned'], 5)
        self.assertIsNone(second['next_offset'])
        self.assertEqual(summary['total_clients'], 30)
        self.assertEqual(len({r['client_id'] for r in first['items'] + second['items']}), 30)
        self.assertEqual(empty['items'], [])
        self.assertIsNone(empty['next_offset'])

    def test_conversation_filters_and_summary_share_scope(self):
        rows = [{'client_id': 27, 'email_address': f'{i}@example.com',
                 'bucket': 'active' if i < 30 else 'storage',
                 'stage': 'lead' if i < 28 else None} for i in range(40)]
        data = {'status': 'ok', 'mailbox_user_id': 27, 'conversations_by_email': rows}
        with patch.object(server, 'get_json', return_value=data):
            page = server.list_conversations(27, bucket='active', stage='lead')
            summary = server.get_conversations_summary(27, bucket='active', stage='lead')
            unknown = server.list_conversations(27, stage='unknown')
        self.assertEqual(page['total'], 28)
        self.assertEqual(page['returned'], 25)
        self.assertEqual(summary['total_rows'], 28)
        self.assertEqual(unknown['total'], 12)

    def test_history_cursor_keeps_events_drafts_and_new_messages(self):
        data = {'timeline': [{'type': 'message' if i % 2 else 'event',
                             'timestamp': 'same', 'payload': {'id': i}} for i in range(45)],
                'pending_approvals': [{'id': 81}], 'pending_questions': [{'id': 7}]}
        with patch.object(server, '_get_conversation', return_value=data):
            first = server.get_conversation(27, 'a@example.com')
            data['timeline'].append({'type': 'message', 'payload': {'id': 45}})
            second = server.get_conversation(27, 'a@example.com', before=first['timeline_page']['next_before'])
            last = server.get_conversation(27, 'a@example.com', before=second['timeline_page']['next_before'])
        ids = [r['payload']['id'] for page in (last, second, first) for r in page['timeline']]
        self.assertEqual(ids, list(range(45)))
        self.assertEqual(first['pending_approvals'], [{'id': 81}])
        self.assertEqual(last['pending_questions'], [{'id': 7}])
        self.assertIsNone(last['timeline_page']['next_before'])

    def test_changed_history_or_selection_rejects_cursor(self):
        data = {'timeline': [{'payload': {'id': i}} for i in range(21)]}
        with patch.object(server, '_get_conversation', return_value=data):
            cursor = server.get_conversation(27, 'a@example.com')['timeline_page']['next_before']
            with self.assertRaises(ToolError):
                server.get_conversation(27, 'b@example.com', before=cursor)
            data['timeline'][0]['payload']['id'] = 999
            with self.assertRaises(ToolError):
                server.get_conversation(27, 'a@example.com', before=cursor)

    def test_empty_history(self):
        with patch.object(server, '_get_conversation', return_value={'timeline': []}):
            page = server.get_conversation(27, 'a@example.com')['timeline_page']
        self.assertEqual(page, {'total': 0, 'returned': 0, 'next_before': None})

    def test_invalid_page_never_calls_api(self):
        with patch.object(server, 'get_json') as read:
            for kwargs in ({'limit': 0}, {'limit': 101}, {'offset': -1}):
                with self.assertRaises(ToolError):
                    server.list_clients(**kwargs)
                with self.assertRaises(ToolError):
                    server.list_conversations(27, **kwargs)
            with self.assertRaises(ToolError):
                server.get_conversation(27, 'a@example.com', limit=0)
        read.assert_not_called()

    def test_attachment_before_first_page_remains_accessible(self):
        data = {'mailbox_user_id': 27, 'client_id': 27, 'selected_email': 'a@example.com',
                'timeline': [{'payload': {'attachments': [{'id': 1, 'filename': 'a.pdf'}]}}]
                            + [{'payload': {}} for _ in range(30)]}
        with patch.object(server, '_get_conversation', return_value=data), \
             patch.object(server, '_download_attachment', return_value=b'data') as download, \
             patch.object(server, 'extract_attachment', return_value={'text': 'Read'}):
            self.assertFalse(any(e['payload'].get('attachments') for e in
                                 server.get_conversation(27, 'a@example.com')['timeline']))
            result = server.read_attachment(27, 'a@example.com', 1)
        download.assert_called_once_with(1)
        self.assertEqual(result['text'], 'Read')


if __name__ == '__main__':
    unittest.main()
