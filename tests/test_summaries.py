import unittest
from unittest.mock import patch

from mcp.server.fastmcp.exceptions import ToolError
import server


class SummaryTests(unittest.TestCase):
    def test_client_scope_and_counter_meaning(self):
        data = {
            'A': {'client_id': 1, 'is_active': True, 'in_progress': True,
                  'pending_questions': 20, 'pending_sales_call': 2},
            'B': {'client_id': 2, 'is_active': True, 'in_progress': True,
                  'pending_questions': 1, 'pending_sales_call_scheduled': 3},
            'Paused': {'client_id': 3, 'is_active': True, 'in_progress': True,
                       'is_paused': True, 'pending_questions': 99},
            'Inactive': {'client_id': 4, 'is_active': False, 'in_progress': True},
        }
        with patch.object(server, 'get_json', return_value=data) as read:
            result = server.get_clients_summary()
        read.assert_called_once_with('/api/statistics')
        self.assertEqual(result['total_clients'], 2)
        self.assertEqual(result['counters']['pending_questions'], {'total': 21, 'rows_with_value': 2})
        self.assertEqual(result['counters']['pending_sales_call_scheduled'], {'total': 3, 'rows_with_value': 1})

    def test_contact_identity_and_repeated_rows(self):
        rows = [
            {'client_id': 1, 'email_address': 'a@example.com', 'bucket': 'active', 'stage': 'lead',
             'pending_envelope_count': 4, 'pending_question_count': 2, 'has_call': True},
            {'client_id': 1, 'email_address': 'a@example.com', 'bucket': 'storage', 'stage': 'lead',
             'pending_question_count': 2},
            {'client_id': 2, 'email_address': 'a@example.com', 'bucket': 'terminated', 'stage': None},
        ]
        with patch.object(server, '_list_conversations', return_value=rows) as read:
            result = server.get_conversations_summary(27, 'company')
        read.assert_called_once_with(27, 'company', None, None)
        self.assertEqual(result['total_rows'], 3)
        self.assertEqual(result['unique_contacts'], 2)
        self.assertEqual(result['by_bucket'], {'active': 1, 'storage': 1, 'terminated': 1})
        self.assertEqual(result['by_stage'], {'lead': 2, 'unknown': 1})
        self.assertEqual(result['row_counters']['pending_question_count']['total'], 4)
        self.assertEqual(result['rows_with_flags']['has_call'], 1)

    def test_empty_lists(self):
        with patch.object(server, '_list_clients', return_value=[]):
            result = server.get_clients_summary()
        self.assertEqual(result['total_clients'], 0)
        self.assertTrue(all(item['total'] == 0 for item in result['counters'].values()))
        with patch.object(server, '_list_conversations', return_value=[]):
            result = server.get_conversations_summary(27)
        self.assertEqual(result['unique_contacts'], 0)
        self.assertEqual(result['by_bucket'], {'active': 0, 'storage': 0, 'terminated': 0})

    def test_query_passes_to_api_and_access_checks_remain(self):
        data = {'status': 'ok', 'mailbox_user_id': 27, 'conversations_by_email': []}
        with patch.object(server, 'get_json', return_value=data) as read:
            server.get_conversations_summary(27, ' a&b ')
        self.assertIn('q=a%26b', read.call_args.args[0])
        with patch.object(server, 'get_json', return_value=dict(data, mailbox_user_id=28)):
            with self.assertRaisesRegex(ToolError, 'different mailbox'):
                server.get_conversations_summary(27)

    def test_bad_counters_do_not_produce_misleading_total(self):
        for value in (-1, '3', 1.5):
            with patch.object(server, '_list_clients', return_value=[{'pending_questions': value}]):
                with self.assertRaisesRegex(ToolError, 'counter value'):
                    server.get_clients_summary()


if __name__ == '__main__':
    unittest.main()
