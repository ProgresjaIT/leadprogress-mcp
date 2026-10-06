import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import threading
import unittest

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import server
from calendar_tools import input_time, stored_time
from test_server import FakePanel, ThreadingHTTPServer


def event(id, time='2026-10-05T10:00:00', **values):
    return {'id': id, 'client_id': 27, 'email_address': 'test@example.com',
            'event_action': 'sales_call_scheduled', 'event_time': time, 'is_done': None, **values}


class CalendarAPI:
    def __init__(self):
        self.salesmen = [{'id': 5, 'name': 'Jan Kowalski', 'clients': [
            {'id': 27, 'username': 'A'}, {'id': 28, 'username': 'B'}]}]
        self.events = [event(1), event(2, 'Mon, 05 Oct 2026 10:30:00 GMT', is_done=False),
                       event(3, is_done=True),
                       event(4, '2026-10-05T08:00:00', event_action='sales_call'),
                       event(5, '2026-10-05T09:00:00', event_action='sales_call', client_id=28, is_done=False),
                       event(6, '2026-10-05T14:00:00', event_action='sales_call', is_done=False)]
        self.absences = [{'id': 10, 'date_from': '2026-10-06', 'date_to': '2026-10-08', 'leave_type': 'l4'}]
        self.writes = []
        self.error = None
        self.bad_confirmation = False

    def read(self, path):
        if path == '/api/calendar/salesmen': return deepcopy(self.salesmen)
        if path == '/api/calendar/5': return deepcopy(self.events)
        if path == '/api/calendar/5/vacations': return deepcopy(self.absences)
        if path.startswith('/crm/partial/chat'):
            return {'status': 'ok', 'mailbox_user_id': 27, 'client_id': 27, 'active_client_id': 27,
                    'selected_email': 'test@example.com', 'selected_thread_id': None,
                    'timeline': [], 'pending_approvals': [], 'pending_questions': [], 'threads_for_selected_email': []}
        raise AssertionError(path)

    def write(self, path, data):
        self.writes.append((path, data))
        if path == '/api/calendar/create':
            row = event(7, data['event_time'], client_id=data['client_id'], email_address=data['email'],
                        is_done=False, comment=data['comment'])
            self.events.append(row)
            return row
        if path == '/api/calendar/update':
            row = next(e for e in self.events if e['id'] == data['event_id'])
            row['event_time'] = data['event_time']
            return {'status': 'ok', 'event': dict(row)}
        if path == '/proxy/call_result':
            row = next(e for e in self.events if e['id'] == data['event_id'])
            row['is_done'] = data['picked_up']
            return {'status': 'ok', 'updated_id': row['id']}
        raise AssertionError(path)

    def handle(self, request):
        if request.method == 'GET':
            return httpx.Response(200, json=self.read(request.url.raw_path.decode()))
        if self.error:
            self.writes.append((request.url.path, json.loads(request.content)))
            if self.error == 'timeout': raise httpx.ReadTimeout('test')
            return httpx.Response(self.error, json={'error': 'test'})
        result = self.write(request.url.path, json.loads(request.content))
        if self.bad_confirmation: result = {'status': 'ok'}
        return httpx.Response(200, json=result)


class CalendarTests(unittest.TestCase):
    def setUp(self):
        server.close_session()
        self.addCleanup(server.close_session)
        self.api = CalendarAPI()
        server._client = httpx.Client(base_url='https://panel.example', transport=httpx.MockTransport(self.api.handle))

    def test_discovery_filters_and_pagination(self):
        self.assertEqual(server.list_salesmen(query='kowal', client_id=28)['items'][0]['id'], 5)
        self.assertEqual(server.list_salesmen(client_id=99)['total'], 0)
        first = server.list_calendar_events(5, '2026-10-05', '2026-10-06', limit=2)
        self.assertEqual(first['total'], 5)
        self.assertEqual(first['next_offset'], 2)
        second = server.list_calendar_events(5, '2026-10-05', '2026-10-06', offset=2)
        self.assertEqual(second['next_offset'], None)
        self.assertEqual(server.list_calendar_events(5, '2026-10-05', '2026-10-06', status='done')['items'][0]['id'], 3)
        self.assertEqual(server.list_calendar_events(5, '2026-10-05', '2026-10-06', client_id=28)['total'], 1)

    def test_summary_and_full_day_ai_counts(self):
        result = server.get_calendar_summary(5, '2026-10-05', '2026-10-06')
        self.assertEqual((result['total'], result['pending'], result['done']), (6, 5, 1))
        result = server.get_salesman_availability(5, '2026-10-05T10:00', '2026-10-05T11:00')
        self.assertEqual(result['pending_ai_calls_by_day'], {'2026-10-05': 3})
        self.assertEqual(result['days_at_ai_limit'], ['2026-10-05'])
        self.assertEqual(result['overlapping_agreed_calls'], [{'event_ids': [1, 2]}])

    def test_absence_end_is_exclusive(self):
        self.assertEqual(server.list_salesman_absences(5, '2026-10-07', '2026-10-08')['total'], 1)
        self.assertEqual(server.list_salesman_absences(5, '2026-10-08', '2026-10-09')['total'], 0)

    def test_writes_create_move_and_record(self):
        created = server.create_calendar_event(5, 27, 'test@example.com', '2026-10-05T08:00:00Z', 'Call')
        self.assertEqual(created['event']['event_time'], '2026-10-05T10:00:00')
        self.assertEqual(created['event']['event_action'], 'sales_call_scheduled')
        moved = server.reschedule_calendar_event(5, 7, '2026-10-06T11:00')
        self.assertEqual(moved['event']['event_time'], '2026-10-06T11:00:00')
        self.assertEqual(server.record_call_result(5, 7, True)['calendar_status'], 'done')
        self.assertEqual(server.record_call_result(5, 7, False)['calendar_status'], 'pending')
        self.assertEqual(len(self.api.writes), 4)

    def test_foreign_and_ai_events_cannot_be_moved(self):
        for id in (99, 4, 3):
            with self.subTest(id=id), self.assertRaises(ToolError):
                server.reschedule_calendar_event(5, id, '2026-10-06T10:00')
        with self.assertRaises(ToolError): server.create_calendar_event(5, 99, 'test@example.com', '2026-10-06T10:00')
        with self.assertRaises(ToolError): server.record_call_result(5, 99, True)
        self.assertEqual(self.api.writes, [])

    def test_invalid_input(self):
        for start, end in [('bad', '2026-10-06'), ('2026-10-06', '2026-10-05')]:
            with self.assertRaises(ToolError): server.list_calendar_events(5, start, end)
        with self.assertRaises(ToolError): server.record_call_result(5, 1, 'false')
        with self.assertRaises(ToolError): server.list_calendar_events(5, '2026-10-05', '2026-10-06', status='bad')
        self.assertEqual(self.api.writes, [])

    def test_backend_errors_and_timeouts_never_retry_writes(self):
        for error in (404, 500, 'timeout'):
            self.api.error = error
            self.api.writes.clear()
            with self.subTest(error=error), self.assertRaises(ToolError):
                server.reschedule_calendar_event(5, 1, '2026-10-06T10:00')
            self.assertEqual(len(self.api.writes), 1)

    def test_unconfirmed_success_is_rejected(self):
        self.api.bad_confirmation = True
        with self.assertRaises(ToolError): server.reschedule_calendar_event(5, 1, '2026-10-06T10:00')
        self.assertEqual(len(self.api.writes), 1)

    def test_dates_preserve_panel_time_and_convert_input_offsets(self):
        self.assertEqual(stored_time('Mon, 05 Oct 2026 10:00:00 GMT').hour, 10)
        self.assertEqual(input_time('2026-07-05T08:00:00Z').hour, 10)
        self.assertEqual(input_time('2026-12-05T08:00:00Z').hour, 9)


class CalendarPanel(FakePanel):
    api = None
    def do_GET(self):
        self.requests.append(('GET', self.path))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(self.api.read(self.path)).encode())

    def do_POST(self):
        if self.path == '/login': return super().do_POST()
        self.requests.append(('POST', self.path))
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        result = self.api.write(self.path, data)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class CalendarMCPTest(unittest.TestCase):
    def test_all_calendar_tools_through_real_stdio(self):
        CalendarPanel.api = CalendarAPI()
        CalendarPanel.requests = []
        panel = ThreadingHTTPServer(('127.0.0.1', 0), CalendarPanel)
        thread = threading.Thread(target=panel.serve_forever, daemon=True)
        thread.start()
        async def run():
            params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__).resolve())],
                env={'LEADPROGRESS_URL': f'http://127.0.0.1:{panel.server_port}',
                     'LEADPROGRESS_USERNAME': 'operator', 'LEADPROGRESS_PASSWORD': 'secret'})
            scope = {'salesman_id': 5, 'date_from': '2026-10-05', 'date_to': '2026-10-09'}
            calls = [('list_salesmen', {}), ('list_calendar_events', scope), ('list_salesman_absences', scope),
                     ('get_calendar_summary', scope), ('get_salesman_availability', scope),
                     ('create_calendar_event', {'salesman_id': 5, 'client_id': 27, 'email': 'test@example.com', 'event_time': '2026-10-05T10:00'}),
                     ('reschedule_calendar_event', {'salesman_id': 5, 'event_id': 7, 'event_time': '2026-10-06T11:00'}),
                     ('record_call_result', {'salesman_id': 5, 'event_id': 7, 'picked_up': True})]
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    for name, args in calls:
                        result = await client.call_tool(name, args)
                        self.assertFalse(result.isError, f'{name}: {result.content}')
                    result = await client.call_tool('list_calendar_events', {**scope, 'status': 'done'})
                    data = result.structuredContent or json.loads(result.content[0].text)
                    self.assertIn(7, [row['id'] for row in data['items']])
        try:
            asyncio.run(asyncio.wait_for(run(), 45))
            self.assertEqual(len(CalendarPanel.api.writes), 3)
        finally:
            panel.shutdown()
            panel.server_close()
            thread.join()
