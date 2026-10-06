"""Calendar MCP tools. Times match Warsaw wall time in the existing panel."""
from collections import Counter
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from mcp.server.fastmcp.exceptions import ToolError

WARSAW = ZoneInfo('Europe/Warsaw')
CALL_TYPES = ('sales_call', 'sales_call_scheduled')


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ToolError(f'{name} must be a positive integer.')


def input_time(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ToolError('Use an ISO date/time, e.g. 2026-10-05T10:00:00 (Europe/Warsaw).') from None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(WARSAW).replace(tzinfo=None)
    return stamp


def stored_time(value):
    # Flask labels naive database times GMT. Preserve wall time rather than
    # converting it to Warsaw, which would shift the time seen in the panel.
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except (ValueError, TypeError):
        try:
            return parsedate_to_datetime(value).replace(tzinfo=None)
        except (ValueError, TypeError, AttributeError):
            raise ToolError('Calendar returned an invalid date/time.') from None


def date_range(date_from, date_to):
    start, end = input_time(date_from), input_time(date_to)
    if end <= start:
        raise ToolError('date_to must be later than date_from; range end is exclusive.')
    return start, end


def register_calendar_tools(mcp, get_json, post_json, get_conversation, validate_page, page):
    def salesmen():
        rows = get_json('/api/calendar/salesmen')
        if not isinstance(rows, list):
            raise ToolError('Unexpected salesmen response. Deploy /api/calendar/salesmen first.')
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or not isinstance(row.get('name'), str) or not isinstance(row.get('clients'), list)
                    or any(not isinstance(c, dict) or type(c.get('id')) is not int for c in row['clients'])):
                raise ToolError('Unexpected calendar salesman metadata.')
        return rows

    def selected_salesman(salesman_id):
        positive(salesman_id, 'salesman_id')
        matches = [row for row in salesmen() if row['id'] == salesman_id]
        if len(matches) != 1:
            raise ToolError('Salesman is not present in the accessible calendar list.')
        return matches[0]

    def events(salesman_id):
        salesman = selected_salesman(salesman_id)
        client_ids = {c['id'] for c in salesman['clients']}
        rows = get_json(f'/api/calendar/{salesman_id}')
        if not isinstance(rows, list):
            raise ToolError('Unexpected calendar events response.')
        result = []
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or row.get('client_id') not in client_ids or row.get('event_action') not in CALL_TYPES
                    or not isinstance(row.get('email_address'), str)
                    or (row.get('is_done') is not None and type(row.get('is_done')) is not bool)):
                raise ToolError('Unexpected calendar event metadata or account.')
            result.append({k: row.get(k) for k in (
                'id', 'client_id', 'username', 'email_address', 'event_action', 'is_done', 'comment', 'thread_id')}
                | {'event_time': stored_time(row.get('event_time')).isoformat(),
                   'status': 'done' if row.get('is_done') is True else 'pending'})
        return sorted(result, key=lambda row: (row['event_time'], row['id']))

    def filter_events(rows, start, end, client_id, email, event_action, status):
        if client_id is not None:
            positive(client_id, 'client_id')
        if event_action is not None and event_action not in CALL_TYPES:
            raise ToolError('event_action must be sales_call or sales_call_scheduled.')
        if status not in ('all', 'pending', 'done'):
            raise ToolError('status must be all, pending or done.')
        return [row for row in rows if start <= stored_time(row['event_time']) < end
                and (client_id is None or row['client_id'] == client_id)
                and (email is None or row['email_address'].casefold() == email.strip().casefold())
                and (event_action is None or row['event_action'] == event_action)
                and (status == 'all' or row['status'] == status)]

    def absences(salesman_id, start, end):
        selected_salesman(salesman_id)
        rows = get_json(f'/api/calendar/{salesman_id}/vacations')
        if not isinstance(rows, list):
            raise ToolError('Unexpected calendar absences response.')
        result = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get('id')) is not int:
                raise ToolError('Unexpected absence metadata.')
            left, right = stored_time(row.get('date_from')), stored_time(row.get('date_to'))
            if right <= left:
                raise ToolError('Unexpected absence date range.')
            if left < end and right > start:
                result.append({'id': row['id'], 'salesman_id': salesman_id, 'leave_type': row.get('leave_type'),
                               'date_from': left.date().isoformat(), 'date_to': right.date().isoformat()})
        return sorted(result, key=lambda row: (row['date_from'], row['id']))

    def chosen_event(salesman_id, event_id):
        positive(event_id, 'event_id')
        matches = [row for row in events(salesman_id) if row['id'] == event_id]
        if len(matches) != 1:
            raise ToolError('Event is not present in the accessible salesman calendar.')
        return matches[0]

    @mcp.tool()
    def list_salesmen(query: str | None = None, client_id: int | None = None,
                     limit: int = 25, offset: int = 0) -> dict:
        """Discover calendar salesman IDs/names and active accounts (clients[].id/username).
        query: name substring; client_id: assigned account. Includes paused/not-running active accounts.
        Returns items,total,next_offset; requires the panel /api/calendar/salesmen endpoint."""
        validate_page(limit, offset)
        if client_id is not None:
            positive(client_id, 'client_id')
        rows = [row for row in salesmen() if (not query or query.strip().casefold() in row['name'].casefold())
                and (client_id is None or any(c['id'] == client_id for c in row['clients']))]
        return page(sorted(rows, key=lambda row: (row['name'], row['id'])), limit, offset)

    @mcp.tool()
    def list_calendar_events(salesman_id: int, date_from: str, date_to: str,
                             client_id: int | None = None, email: str | None = None,
                             event_action: str | None = None, status: str = 'pending',
                             limit: int = 25, offset: int = 0) -> dict:
        """List calls across active accounts of a salesman; ISO Warsaw dates, end exclusive.
        status: pending (NULL/FALSE), done (TRUE), all. event_action: sales_call/sales_call_scheduled.
        Account/contact filters, pagination; returned event_time is ISO Warsaw wall time (no offset)."""
        validate_page(limit, offset)
        start, end = date_range(date_from, date_to)
        rows = filter_events(events(salesman_id), start, end, client_id, email, event_action, status)
        return {'timezone': 'Europe/Warsaw', **page(rows, limit, offset)}

    @mcp.tool()
    def list_salesman_absences(salesman_id: int, date_from: str, date_to: str,
                              limit: int = 25, offset: int = 0) -> dict:
        """Read overlapping holidays/L4; ISO Warsaw dates, range and absence end exclusive.
        Read-only; returns items,total,next_offset. Does not create/delete absences."""
        validate_page(limit, offset)
        start, end = date_range(date_from, date_to)
        return {'timezone': 'Europe/Warsaw', **page(absences(salesman_id, start, end), limit, offset)}

    @mcp.tool()
    def get_calendar_summary(salesman_id: int, date_from: str, date_to: str) -> dict:
        """Count ALL calls in a Warsaw range (end exclusive), independently of pagination.
        Includes pending/done, pending overdue (before now), by action and pending AI calls by day.
        AI limit 3/day applies to sales_call across accounts, not agreed sales_call_scheduled."""
        start, end = date_range(date_from, date_to)
        rows = filter_events(events(salesman_id), start, end, None, None, None, 'all')
        pending = [row for row in rows if row['status'] == 'pending']
        now = datetime.now(WARSAW).replace(tzinfo=None)
        return {'timezone': 'Europe/Warsaw', 'total': len(rows), 'pending': len(pending),
                'done': len(rows) - len(pending),
                'overdue': sum(stored_time(row['event_time']) < now for row in pending),
                'by_action': dict(Counter(row['event_action'] for row in rows)),
                'pending_ai_calls_by_day': dict(Counter(row['event_time'][:10] for row in pending
                                                      if row['event_action'] == 'sales_call'))}

    @mcp.tool()
    def get_salesman_availability(salesman_id: int, date_from: str, date_to: str,
                                 assumed_call_minutes: int = 60) -> dict:
        """Read pending calls, holidays/L4, overlapping agreed calls and AI daily counts across accounts.
        ISO Warsaw dates, end exclusive. No stored duration: overlaps assume assumed_call_minutes (1-480).
        Informational only; LLM chooses slots and avoids absences/weekends. No guaranteed free slots.
        AI limit 3/day is separate from agreed calls. Counts cover entire days even for partial-day requests."""
        if type(assumed_call_minutes) is not int or not 1 <= assumed_call_minutes <= 480:
            raise ToolError('assumed_call_minutes must be between 1 and 480.')
        start, end = date_range(date_from, date_to)
        duration = timedelta(minutes=assumed_call_minutes)
        all_rows = events(salesman_id)
        rows = [row for row in all_rows if row['status'] == 'pending'
                and stored_time(row['event_time']) < end and stored_time(row['event_time']) + duration > start]
        agreed = [row for row in rows if row['event_action'] == 'sales_call_scheduled']
        overlaps = [{'event_ids': [left['id'], right['id']]} for i, left in enumerate(agreed)
                    for right in agreed[i+1:]
                    if stored_time(right['event_time']) < stored_time(left['event_time']) + duration]
        day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = end if end.time() == datetime.min.time() else end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ai_rows = filter_events(all_rows, day_start, day_end, None, None, 'sales_call', 'pending')
        counts = Counter(row['event_time'][:10] for row in ai_rows)
        return {'timezone': 'Europe/Warsaw', 'assumed_call_minutes': assumed_call_minutes,
                'calls': rows, 'absences': absences(salesman_id, start, end), 'overlapping_agreed_calls': overlaps,
                'ai_daily_limit': 3, 'pending_ai_calls_by_day': dict(counts),
                'days_at_ai_limit': sorted(day for day, count in counts.items() if count >= 3),
                'informational_only': True}

    @mcp.tool()
    def create_calendar_event(salesman_id: int, client_id: int, email: str,
                              event_time: str, comment: str | None = None) -> dict:
        """Create an agreed call (sales_call_scheduled), never an AI-imposed sales_call.
        ISO Warsaw wall time, or offset converted to Warsaw. Conflicts are not blocked; read availability
        first and avoid holidays/L4. Validates account/contact; creates an unthreaded event.
        May trigger existing SMS reminders to salesman. No retry; inspect calendar after errors."""
        positive(client_id, 'client_id')
        stamp = input_time(event_time).replace(microsecond=0)
        salesman = selected_salesman(salesman_id)
        if client_id not in {c['id'] for c in salesman['clients']}:
            raise ToolError('Account is not assigned to this salesman.')
        chat = get_conversation(client_id, email)
        row = post_json('/api/calendar/create', {'client_id': client_id, 'email': chat['selected_email'],
                        'event_time': stamp.strftime('%Y-%m-%d %H:%M:%S'), 'comment': comment}, None)
        if (type(row.get('id')) is not int or row['id'] <= 0 or row.get('client_id') != client_id
                or row.get('email_address') != chat['selected_email'] or row.get('event_action') != 'sales_call_scheduled'
                or stored_time(row.get('event_time')) != stamp):
            raise ToolError('Created event was not confirmed. Check calendar before retrying.')
        return {'status': 'created', 'timezone': 'Europe/Warsaw', 'event': row | {'event_time': stored_time(row['event_time']).isoformat()}}

    @mcp.tool()
    def reschedule_calendar_event(salesman_id: int, event_id: int, event_time: str) -> dict:
        """Move a pending agreed call only; never moves AI calls (their 3/day planning is unchanged).
        ISO Warsaw time; read availability/absences first. Overlaps are informational, not blocked.
        May affect reminders. Write never retried; inspect calendar after errors."""
        stamp = input_time(event_time).replace(microsecond=0)
        current = chosen_event(salesman_id, event_id)
        if current['event_action'] != 'sales_call_scheduled' or current['status'] != 'pending':
            raise ToolError('Only pending agreed calls can be rescheduled with this tool.')
        result = post_json('/api/calendar/update', {'event_id': event_id,
                           'event_time': stamp.strftime('%Y-%m-%d %H:%M:%S')}, 'ok')
        row = result.get('event')
        if (not isinstance(row, dict) or row.get('id') != event_id
                or row.get('client_id') != current['client_id'] or row.get('email_address') != current['email_address']
                or stored_time(row.get('event_time')) != stamp):
            raise ToolError('Updated event was not confirmed. Check calendar before retrying.')
        return {'status': 'updated', 'timezone': 'Europe/Warsaw', 'event': row | {'event_time': stored_time(row['event_time']).isoformat()}}

    @mcp.tool()
    def record_call_result(salesman_id: int, event_id: int, picked_up: bool) -> dict:
        """Record actual call result: true completes it, false keeps it pending and clears done_at.
        Supports agreed and AI calls from this salesman calendar. Does not send SMS/mail.
        Write never retried; inspect calendar after errors."""
        if type(picked_up) is not bool:
            raise ToolError('picked_up must be a boolean.')
        chosen_event(salesman_id, event_id)
        result = post_json('/proxy/call_result', {'event_id': event_id, 'picked_up': picked_up}, 'ok')
        if result.get('updated_id') != event_id:
            raise ToolError('Call result was not confirmed. Check calendar before retrying.')
        return {'status': 'ok', 'event_id': event_id, 'picked_up': picked_up,
                'calendar_status': 'done' if picked_up else 'pending'}

    return {fn.__name__: fn for fn in (list_salesmen, list_calendar_events, list_salesman_absences,
            get_calendar_summary, get_salesman_availability, create_calendar_event,
            reschedule_calendar_event, record_call_result)}
