import atexit
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from threading import RLock
from urllib.parse import urljoin, urlparse

import httpx
from attachments import AttachmentError, MAX_FILE_BYTES, extract_attachment, file_format
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

load_dotenv(Path(__file__).with_name('.env'))
mcp = FastMCP('Leadprogress')
_client: httpx.Client | None = None
_session_lock = RLock()


def close_session():
    """Close the shared HTTP client when the server exits."""
    global _client
    with _session_lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_session)


def login() -> httpx.Client:
    """Return the shared authenticated client; log in only when needed."""
    global _client
    with _session_lock:
        if _client is not None:
            return _client
        url = os.getenv('LEADPROGRESS_URL', '').rstrip('/')
        username = os.getenv('LEADPROGRESS_USERNAME', '')
        password = os.getenv('LEADPROGRESS_PASSWORD', '')
        if not all((url, username, password)):
            raise ToolError('Set LEADPROGRESS_URL, LEADPROGRESS_USERNAME and LEADPROGRESS_PASSWORD.')
        parsed = urlparse(url)
        local_http = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if (not parsed.hostname or (parsed.scheme != 'https' and not local_http)
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ToolError('LEADPROGRESS_URL must be an HTTPS origin (HTTP allowed for localhost).')

        client = httpx.Client(base_url=url, timeout=30, follow_redirects=False)
        try:
            response = client.post('/login', data={'username': username, 'password': password})
            if response.status_code != 302 or not client.cookies:
                raise ToolError('Login failed. Check the configured account credentials.')
        except Exception:
            client.close()
            raise
        _client = client
        return client


def get_json(path: str):
    """Read a panel endpoint, retrying once after an expired login session."""
    if not path.startswith('/') or path.startswith('//') or '\\' in path:
        raise ValueError('Use an absolute panel path, e.g. /api/statistics.')
    with _session_lock:
        try:
            for attempt in range(2):
                client = login()
                response = client.get(path, headers={'Accept': 'application/json'})
                target = urlparse(urljoin(str(response.url), response.headers.get('Location', '')))
                origin = urlparse(str(client.base_url))
                expired = response.status_code == 401 or (
                    response.status_code in (302, 303)
                    and (target.scheme, target.netloc, target.path) == (origin.scheme, origin.netloc, '/login')
                )
                if expired:
                    close_session()
                    if attempt == 0:
                        continue
                    raise ToolError('Session rejected after login. Check account permissions.')
                if response.status_code == 403:
                    raise ToolError('Access denied. Use an administrator or operator account.')
                if response.status_code != 200:
                    raise ToolError(f'{path} returned HTTP {response.status_code}.')
                return response.json()
        except httpx.RequestError:
            raise ToolError('Cannot connect to Leadprogress. Check the URL and network.') from None
        except ValueError:
            raise ToolError(f'{path} did not return valid JSON.') from None


def _list_clients(query: str | None = None, salesman: str | None = None) -> list[dict]:
    """List active, running, unpaused Leadprogress clients and summary statistics."""
    data = get_json('/api/statistics')
    if not isinstance(data, dict) or any(
        not isinstance(row, dict) or type(row.get('client_id')) is not int
        for row in data.values()
    ):
        raise ToolError('Unexpected /api/statistics response format.')
    fields = (
        'description', 'client_type', 'salesman',
        'sent_total', 'interested_total', 'hasUncheckedErrors',
        'pending_questions', 'pending_sales_call', 'pending_sales_call_scheduled',
    )
    return [
        {
            'client_id': row['client_id'],
            'username': name,
            **{field: row.get(field) for field in fields},
        }
        for name, row in data.items()
        if row.get('in_progress') and row.get('is_active') and not row.get('is_paused')
        and (not query or query.strip().casefold() in name.casefold())
        and (not salesman or salesman.strip().casefold() == (row.get('salesman') or '').casefold())
    ]


def _list_conversations(client_id: int, query: str | None = None,
                        bucket: str | None = None, stage: str | None = None) -> list[dict]:
    """List CRM contacts, statuses and counters for mailbox client_id, without history.
    Optional query searches email, phone, company and message text.
    Includes all buckets; returned client_id may identify another conversation owner."""
    if client_id <= 0:
        raise ToolError('client_id must be a positive integer.')
    if bucket is not None and bucket not in ('active', 'storage', 'terminated'):
        raise ToolError('bucket must be active, storage or terminated.')
    params = {'user_id': client_id, 'format': 'json'}
    if query and query.strip():
        params['q'] = query.strip()
    data = get_json('/crm?' + str(httpx.QueryParams(params)))
    if not isinstance(data, dict) or data.get('status') != 'ok':
        raise ToolError('Unexpected /crm response format.')
    if data.get('mailbox_user_id') != client_id:
        raise ToolError('CRM returned a different mailbox. Check account permissions.')
    conversations = data.get('conversations_by_email')
    if not isinstance(conversations, list) or any(
        not isinstance(row, dict)
        or type(row.get('client_id')) is not int
        or not isinstance(row.get('email_address'), str)
        for row in conversations
    ):
        raise ToolError('Unexpected /crm conversation list format.')
    return [row for row in conversations
            if (bucket is None or row.get('bucket') == bucket)
            and (stage is None or (row.get('stage') or 'unknown') == stage)]


def _validate_page(limit: int, offset: int = 0):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolError('limit must be between 1 and 100.')
    if type(offset) is not int or offset < 0:
        raise ToolError('offset must be a non-negative integer.')


def _page(rows: list[dict], limit: int, offset: int) -> dict:
    items = rows[offset:offset + limit]
    return {'total': len(rows), 'returned': len(items), 'items': items,
            'next_offset': offset + len(items) if offset + len(items) < len(rows) else None}


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def list_clients(limit: int = 25, offset: int = 0,
                 query: str | None = None, salesman: str | None = None) -> dict:
    """List active, running, unpaused clients; client_id can be used as mailbox ID.
    query: name substring; salesman: exact name, both case-insensitive.
    Returns items, total after filters, returned, next_offset. limit 1-100 (default 25).
    Pass next_offset as offset with unchanged filters; null means done. Use summary for counts."""
    _validate_page(limit, offset)
    rows = _list_clients(query, salesman)
    rows.sort(key=lambda row: (row['username'].casefold(), row['client_id']))
    return _page(rows, limit, offset)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def list_client_attachments(client_id: int, query: str | None = None,
                            limit: int = 25, offset: int = 0) -> dict:
    """Browse the client's outgoing attachment library, as shown in the client panel.
    client_id: ID from list_clients (active, running, unpaused accounts only).
    Includes files not attached to any conversation/draft; excludes the separate contracts module.
    query: case-insensitive filename/description substring. Returns metadata only, not file text.
    Returns items,total,returned,next_offset; pass next_offset with unchanged filters for more.
    Library-only IDs cannot be read by read_attachment until attached to a conversation/draft."""
    if type(client_id) is not int or client_id <= 0:
        raise ToolError('client_id must be a positive integer.')
    _validate_page(limit, offset)
    if not any(row['client_id'] == client_id for row in _list_clients()):
        raise ToolError('Client is not present in the accessible active CRM accounts.')
    data = get_json(f'/proxy/clients/{client_id}/available_attachments?type=out_going_to_client')
    if not isinstance(data, dict) or not isinstance(data.get('attachments'), list):
        raise ToolError('Unexpected client attachment library response format.')
    rows = data['attachments']
    if any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] <= 0
           or not isinstance(row.get('filename'), str) or not row['filename'].strip()
           or (row.get('description') is not None and not isinstance(row['description'], str))
           for row in rows):
        raise ToolError('Unexpected client attachment metadata.')
    if len({row['id'] for row in rows}) != len(rows):
        raise ToolError('Duplicate client attachment IDs.')
    query = (query or '').strip().casefold()
    rows = [row for row in rows if row.get('type') == 'out_going_to_client'
            and (not query or query in row['filename'].casefold()
                 or query in (row.get('description') or '').casefold())]
    rows.sort(key=lambda row: row['id'])
    fields = ('id', 'filename', 'description', 'mime_type', 'size', 'type')
    rows = [{key: row[key] for key in fields if key in row} for row in rows]
    return {'client_id': client_id, **_page(rows, limit, offset)}


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def list_conversations(client_id: int, query: str | None = None,
                       bucket: str | None = None, stage: str | None = None,
                       limit: int = 25, offset: int = 0) -> dict:
    """List CRM contacts for mailbox client_id from list_clients or a CRM user_id URL.
    query searches email/phone/company/text; bucket: active/storage/terminated;
    stage: exact value or unknown (missing). Omit filters for all groups.
    Returns items, total after filters, returned, next_offset; limit 1-100 (default 25).
    Pass next_offset as offset with unchanged filters; null means done.
    For chat tools use row.email_address as email and row.client_id as conversation_client_id."""
    _validate_page(limit, offset)
    rows = _list_conversations(client_id, query, bucket, stage)
    rows.sort(key=lambda row: (row['client_id'], row['email_address'], row.get('bucket') or '',
                               str(row.get('group_key') or ''), str(row.get('default_thread_id') or '')))
    return _page(rows, limit, offset)


def _counter_summary(rows: list[dict], fields: tuple[str, ...]) -> dict:
    result = {}
    for field in fields:
        values = [row.get(field) or 0 for row in rows]
        if any(type(value) is not int or value < 0 for value in values):
            raise ToolError(f'Unexpected counter value: {field}.')
        result[field] = {'total': sum(values), 'rows_with_value': sum(value > 0 for value in values)}
    return result


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def get_clients_summary(query: str | None = None, salesman: str | None = None) -> dict:
    """Count ALL matching active, running, unpaused clients, independent of pagination.
    query: name substring; salesman: exact name, both case-insensitive.
    pending_questions=envelopes, pending_sales_call=megaphones, pending_sales_call_scheduled=phones.
    Each counter has total (sum) and rows_with_value (clients with positive value)."""
    rows = _list_clients(query, salesman)
    return {'total_clients': len(rows), 'counters': _counter_summary(rows, (
        'pending_questions', 'pending_sales_call', 'pending_sales_call_scheduled'))}


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def get_conversations_summary(client_id: int, query: str | None = None,
                              bucket: str | None = None, stage: str | None = None) -> dict:
    """Count ALL matching CRM list rows for mailbox client_id, independent of pagination.
    Filters as list_conversations: query, bucket (active/storage/terminated), stage (unknown if missing).
    Returns total_rows, unique_contacts (owner/email pairs), by_bucket, by_stage.
    row_counters sum rows; contact-wide values may repeat across groups. Flags count rows, not events."""
    rows = _list_conversations(client_id, query, bucket, stage)
    buckets = dict.fromkeys(('active', 'storage', 'terminated'), 0)
    buckets.update(Counter(row.get('bucket') or 'unknown' for row in rows))
    return {
        'mailbox_user_id': client_id,
        'query': query.strip() if query and query.strip() else None,
        'total_rows': len(rows),
        'unique_contacts': len({(row['client_id'], row['email_address']) for row in rows}),
        'by_bucket': buckets,
        'by_stage': dict(Counter(row.get('stage') or 'unknown' for row in rows)),
        'row_counters': _counter_summary(rows, (
            'pending_envelope_count', 'pending_question_count', 'unprocessed_count')),
        'rows_with_flags': {field: sum(bool(row.get(field)) for row in rows)
                            for field in ('has_call', 'has_call_scheduled', 'is_important')},
    }


def _get_conversation(
    client_id: int,
    email: str,
    thread_id: int | None = None,
    bucket: str | None = None,
    conversation_client_id: int | None = None,
) -> dict:
    """Read messages, events, notes, unsent drafts and AI questions.
    client_id: mailbox; email: email_address from list_conversations.
    conversation_client_id: row's client_id when different from mailbox.
    thread_id: omit for contact history, 0 for unthreaded, otherwise thread ID.
    bucket: active/storage/terminated; selects thread group, not history filter.
    Includes message attachment metadata; excludes files and draft attachments."""
    if client_id <= 0 or (conversation_client_id is not None and conversation_client_id <= 0):
        raise ToolError('Client IDs must be positive integers.')
    email = email.strip()
    if not email:
        raise ToolError('email must not be empty; use email_address from list_conversations.')
    if thread_id is not None and thread_id < 0:
        raise ToolError('thread_id must be non-negative (0 means no thread).')
    if bucket is not None and bucket not in ('active', 'storage', 'terminated'):
        raise ToolError('bucket must be active, storage or terminated.')

    params = {'user_id': client_id, 'email': email, 'format': 'json'}
    for name, value in (('thread_id', thread_id), ('bucket', bucket),
                        ('conversation_client_id', conversation_client_id)):
        if value is not None:
            params[name] = value
    data = get_json('/crm/partial/chat?' + str(httpx.QueryParams(params)))
    if not isinstance(data, dict) or data.get('status') != 'ok':
        raise ToolError('Unexpected CRM chat response format.')
    if data.get('mailbox_user_id') != client_id:
        raise ToolError('CRM returned a different mailbox. Check account permissions.')
    expected_client_id = conversation_client_id if conversation_client_id is not None else client_id
    if (data.get('client_id') != expected_client_id
            or data.get('active_client_id') != expected_client_id
            or data.get('selected_email') != email):
        raise ToolError('CRM did not select the requested conversation. Check identifiers and permissions.')
    if thread_id is not None and data.get('selected_thread_id') != thread_id:
        raise ToolError('CRM did not select the requested thread. Refresh the conversation and choose an available thread.')
    for field in ('timeline', 'threads_for_selected_email', 'pending_approvals', 'pending_questions'):
        if not isinstance(data.get(field), list) or any(not isinstance(item, dict) for item in data[field]):
            raise ToolError(f'Unexpected CRM chat field: {field}.')
    return data


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def get_conversation(
    client_id: int, email: str, thread_id: int | None = None,
    bucket: str | None = None, conversation_client_id: int | None = None,
    limit: int = 20, before: str | None = None,
) -> dict:
    """Read timeline messages/events/notes, all pending_approvals (drafts) and pending_questions.
    client_id: mailbox; email: row.email_address; conversation_client_id: row.client_id from list_conversations.
    thread_id: omit for contact history, 0 for unthreaded, positive ID from threads_for_selected_email.
    bucket: active/storage/terminated; selects thread group, not history filter.
    Latest 20 items by default (limit 1-100), oldest first within page. Pass timeline_page.next_before
    as before with unchanged selection for older items; null means done. On stale cursor restart.
    Message attachments are in timeline[].payload.attachments; use list_draft_attachments for draft files."""
    _validate_page(limit)
    data = _get_conversation(client_id, email, thread_id, bucket, conversation_client_id)
    timeline = data['timeline']
    scope = [client_id, email.strip(), thread_id, bucket, conversation_client_id,
             data.get('selected_thread_id')]

    def cursor(index):
        value = json.dumps([scope, timeline[:index]], sort_keys=True, ensure_ascii=False)
        return f'{index}:' + hashlib.sha256(value.encode('utf-8')).hexdigest()

    end = len(timeline)
    if before is not None:
        try:
            end = int(before.split(':', 1)[0])
        except (ValueError, AttributeError):
            raise ToolError('Invalid before cursor.') from None
        if not 0 <= end <= len(timeline) or before != cursor(end):
            raise ToolError('History or selection changed, or invalid cursor. Restart without before.')
    start = max(0, end - limit)
    return {**data, 'timeline': timeline[start:end], 'timeline_page': {
        'total': len(timeline), 'returned': end - start,
        'next_before': cursor(start) if start else None,
    }}


def _download_attachment(attachment_id: int) -> bytes:
    with _session_lock:
        try:
            for attempt in range(2):
                client = login()
                with client.stream('GET', f'/proxy/attachment/{attachment_id}/download') as response:
                    target = urlparse(urljoin(str(response.url), response.headers.get('Location', '')))
                    origin = urlparse(str(client.base_url))
                    expired = response.status_code == 401 or (
                        response.status_code in (302, 303)
                        and (target.scheme, target.netloc, target.path) == (origin.scheme, origin.netloc, '/login')
                    )
                    if expired:
                        close_session()
                        if attempt == 0:
                            continue
                        raise ToolError('Attachment access rejected after login.')
                    if response.status_code != 200:
                        raise ToolError(f'Attachment download returned HTTP {response.status_code}.')
                    length = response.headers.get('Content-Length', '')
                    if length.isdigit() and int(length) > MAX_FILE_BYTES:
                        raise ToolError(f'Attachment exceeds the {MAX_FILE_BYTES / (1024**2):.1f} MiB limit.')
                    data = bytearray()
                    for chunk in response.iter_bytes(65536):
                        if len(data) + len(chunk) > MAX_FILE_BYTES:
                            raise ToolError(f'Attachment exceeds the {MAX_FILE_BYTES / (1024**2):.1f} MiB limit.')
                        data.extend(chunk)
                    return bytes(data)
        except httpx.RequestError:
            raise ToolError('Cannot download attachment. Check the connection to Leadprogress.') from None


def _get_draft_attachments(conversation: dict, draft_id: int) -> list[dict]:
    if draft_id <= 0:
        raise ToolError('draft_id must be a positive integer.')
    drafts = [row for row in conversation['pending_approvals'] if row.get('id') == draft_id]
    if len(drafts) != 1 or drafts[0].get('email_address') != conversation['selected_email']:
        raise ToolError('Draft is not present in the accessible conversation.')
    data = get_json(f'/proxy/drafts/{draft_id}/attachments')
    if not isinstance(data, dict) or data.get('status') != 'ok':
        raise ToolError('Unexpected draft attachments response format.')
    items = data.get('attachments')
    if (not isinstance(items, list) or any(
            not isinstance(item, dict) or type(item.get('id')) is not int or item['id'] <= 0
            or not isinstance(item.get('filename'), str) or not item['filename'].strip()
            for item in items)):
        raise ToolError('Unexpected draft attachments metadata.')
    if len({item['id'] for item in items}) != len(items):
        raise ToolError('Duplicate draft attachment IDs.')
    return [{key: item[key] for key in ('id', 'filename', 'mime_type', 'size', 'position') if key in item}
            for item in items]


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def list_draft_attachments(
    client_id: int, email: str, draft_id: int,
    conversation_client_id: int | None = None, thread_id: int | None = None,
) -> dict:
    """List unsent draft files (id, filename, mime_type, size, position); never sends mail.
    draft_id: get_conversation.pending_approvals[].id. Use the same mailbox client_id,
    email, conversation_client_id and optional thread_id as that chat.
    Read supported file text with read_attachment using both draft_id and attachment_id."""
    if draft_id <= 0:
        raise ToolError('draft_id must be a positive integer.')
    conversation = _get_conversation(client_id, email, thread_id=thread_id,
                                    conversation_client_id=conversation_client_id)
    attachments = _get_draft_attachments(conversation, draft_id)
    return {'status': 'ok', 'draft_id': draft_id, 'attachments': attachments,
            'mailbox_user_id': conversation['mailbox_user_id'],
            'client_id': conversation['client_id'], 'email': conversation['selected_email']}


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
))
def read_attachment(
    client_id: int,
    email: str,
    attachment_id: int,
    conversation_client_id: int | None = None,
    thread_id: int | None = None,
    max_chars: int = 30_000,
    draft_id: int | None = None,
) -> dict:
    """Extract ODT/PDF/DOCX text; attachment_id is timeline[].payload.attachments[].id from get_conversation.
    Use the same mailbox client_id, email, conversation_client_id and optional thread_id as that chat.
    Can read attachments from older pages. Max 10 MiB, first 100 PDF pages, max_chars 1-100000 (default 30000).
    For draft files, pass draft_id and attachment_id from list_draft_attachments; validates draft membership.
    Returns text, truncated, warnings; no_text means no extractable text. No OCR/images or disk writes."""
    if attachment_id <= 0:
        raise ToolError('attachment_id must be a positive integer.')
    if not 1 <= max_chars <= 100_000:
        raise ToolError('max_chars must be between 1 and 100_000.')
    if draft_id is not None and draft_id <= 0:
        raise ToolError('draft_id must be a positive integer.')
    conversation = _get_conversation(client_id, email, thread_id=thread_id,
                                    conversation_client_id=conversation_client_id)
    attachment = None
    if draft_id is not None:
        attachment = next((item for item in _get_draft_attachments(conversation, draft_id)
                           if item['id'] == attachment_id), None)
    for event in conversation['timeline'] if draft_id is None else []:
        payload = event.get('payload')
        if isinstance(payload, dict):
            for item in payload.get('attachments') or []:
                if isinstance(item, dict) and item.get('id') == attachment_id:
                    attachment = item
                    break
        if attachment is not None:
            break
    if attachment is None:
        raise ToolError('Attachment is not present in the accessible draft.' if draft_id is not None
                        else 'Attachment is not present in the accessible conversation.')
    filename = attachment.get('filename')
    if not isinstance(filename, str) or not filename:
        raise ToolError('Attachment has no valid filename.')
    try:
        file_format(filename)
        if isinstance(attachment.get('size'), (int, float)) and attachment['size'] > MAX_FILE_BYTES:
            raise AttachmentError(f'Attachment exceeds the {MAX_FILE_BYTES / (1024**2):.1f} MiB limit.')
        data = _download_attachment(attachment_id)
        result = extract_attachment(filename, data, max_chars)
    except AttachmentError as exc:
        raise ToolError(str(exc)) from None
    return {'attachment_id': attachment_id, 'filename': filename, 'size_bytes': len(data),
            **({'draft_id': draft_id} if draft_id is not None else {}),
            'mailbox_user_id': conversation['mailbox_user_id'],
            'client_id': conversation['client_id'], 'email': conversation['selected_email'], **result}


def _post_json(path: str, payload: dict, expected_status: str | None) -> dict:
    """Perform one write; never replay an uncertain operation."""
    with _session_lock:
        client = login()
        try:
            response = client.post(path, json=payload, headers={'Accept': 'application/json'})
        except httpx.RequestError:
            raise ToolError('Write outcome unknown. Check CRM before retrying; request was not repeated.') from None
        if response.status_code in (401, 302, 303):
            close_session()
            raise ToolError('Session rejected the write. Request was not repeated; check CRM before retrying.')
        if response.status_code >= 500:
            raise ToolError(f'Write outcome unknown (HTTP {response.status_code}). Check CRM before retrying.')
        if response.status_code != 200:
            raise ToolError(f'CRM write returned HTTP {response.status_code}; request was not repeated.')
        try:
            data = response.json()
        except ValueError:
            raise ToolError('Write outcome unknown: invalid JSON. Check CRM before retrying.') from None
        if not isinstance(data, dict) or (expected_status is not None and data.get('status') != expected_status):
            raise ToolError('CRM did not confirm the write. Check CRM before retrying.')
        return data


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False,
))
def create_reply_draft(
    client_id: int,
    email: str,
    subject: str,
    body: str,
    thread_id: int | None = None,
    conversation_client_id: int | None = None,
) -> dict:
    """Create an unsent draft; return draft_id for update/send. Never sends mail.
    client_id: mailbox; email: row.email_address; conversation_client_id: row.client_id from list_conversations.
    thread_id: positive ID from get_conversation.threads_for_selected_email; omit to create a NEW thread (0 unsupported).
    subject/body: nonempty plain text; no attachments. Writes are not retried.
    On errors inspect CRM first; creation may succeed before content save fails."""
    if client_id <= 0 or (conversation_client_id is not None and conversation_client_id <= 0):
        raise ToolError('Client IDs must be positive integers.')
    email, subject, body = email.strip(), subject.strip(), body.strip()
    if not email or not subject or not body:
        raise ToolError('email, subject and body must not be empty.')
    if '\r' in email or '\n' in email or '\r' in subject or '\n' in subject:
        raise ToolError('email and subject must not contain line breaks.')
    if thread_id is not None and thread_id <= 0:
        raise ToolError('Use a positive thread_id or omit it for a new thread; legacy thread 0 is unsupported.')
    owner_id = conversation_client_id if conversation_client_id is not None else client_id
    with _session_lock:
        contacts = _list_conversations(client_id)
        if not any(row['client_id'] == owner_id and row['email_address'] == email for row in contacts):
            raise ToolError('Contact is not present in the accessible CRM mailbox.')
        # Reuse CRM selection checks, including thread visibility and ownership.
        _get_conversation(client_id, email, thread_id=thread_id, conversation_client_id=owner_id)
        created = _post_json('/proxy/create_draft', {
            'client_id': owner_id, 'sales_user_id': client_id,
            'email_address': email, 'thread_id': thread_id,
        }, 'ok')
        draft_id = created.get('draft_id')
        if type(draft_id) is not int or draft_id <= 0:
            raise ToolError('CRM may have created a draft but returned no valid ID. Check CRM before retrying.')
        try:
            _post_json(f'/proxy/save_message/{draft_id}', {'subject': subject, 'body': body}, 'saved')
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ToolError) else 'Unexpected error while saving.'
            raise ToolError(f'Draft {draft_id} was created, but content save was not confirmed. '
                            f'Do not create another draft; check this draft in CRM. {detail}') from None
    return {'status': 'draft_created', 'draft_id': draft_id, 'mailbox_user_id': client_id,
            'client_id': owner_id, 'email': email, 'subject': subject, 'body': body,
            'new_thread_created': thread_id is None, 'sent': False}



@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True,
    idempotentHint=False, openWorldHint=False,
))
def update_reply_draft(
    client_id: int,
    email: str,
    draft_id: int,
    subject: str | None = None,
    body: str | None = None,
    conversation_client_id: int | None = None,
    thread_id: int | None = None,
) -> dict:
    """Update draft_id from get_conversation.pending_approvals[].id; never sends mail.
    Use the same mailbox client_id, email, conversation_client_id and optional thread_id as that chat.
    Provide nonempty subject and/or body; supplied fields replace text, omitted fields keep current values.
    Returns saved subject/body. Writes are not retried; after errors inspect CRM before retrying."""
    if draft_id <= 0:
        raise ToolError('draft_id must be a positive integer.')
    if subject is None and body is None:
        raise ToolError('Provide subject and/or body.')
    if subject is not None and (not subject.strip() or '\r' in subject or '\n' in subject):
        raise ToolError('subject must not be empty or contain line breaks.')
    if body is not None and not body.strip():
        raise ToolError('body must not be empty.')
    with _session_lock:
        conversation = _get_conversation(client_id, email, thread_id=thread_id,
                                        conversation_client_id=conversation_client_id)
        drafts = [row for row in conversation['pending_approvals'] if row.get('id') == draft_id]
        if len(drafts) != 1 or drafts[0].get('email_address') != conversation['selected_email']:
            raise ToolError('Draft is not present in the accessible conversation.')
        draft = drafts[0]
        values = {'subject': draft.get('subject') if subject is None else subject,
                  'body': draft.get('body') if body is None else body}
        if any(not isinstance(value, str) for value in values.values()):
            raise ToolError('Draft has an invalid subject or body. Refresh the conversation.')
        values = {key: value.strip() for key, value in values.items()}
        _post_json(f'/proxy/save_message/{draft_id}', values, 'saved')
    return {'status': 'draft_updated', 'draft_id': draft_id,
            'mailbox_user_id': conversation['mailbox_user_id'],
            'client_id': conversation['client_id'], 'email': conversation['selected_email'],
            **values, 'sent': False}


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True,
    idempotentHint=False, openWorldHint=True,
))
def send_reply_draft(
    client_id: int,
    email: str,
    draft_id: int,
    conversation_client_id: int | None = None,
    thread_id: int | None = None,
) -> dict:
    """Send draft_id from get_conversation.pending_approvals[].id with current saved content and all attached files.
    Use the same mailbox client_id, email, conversation_client_id and optional thread_id as that chat.
    Requires explicit user intent to send. Success returns sent=true; backend removes draft and adds history.
    Never retry automatically: after errors check CRM, as mail may already be sent. sent is not delivery confirmation."""
    if draft_id <= 0:
        raise ToolError('draft_id must be a positive integer.')
    with _session_lock:
        conversation = _get_conversation(client_id, email, thread_id=thread_id,
                                        conversation_client_id=conversation_client_id)
        drafts = [row for row in conversation['pending_approvals'] if row.get('id') == draft_id]
        if len(drafts) != 1 or drafts[0].get('email_address') != conversation['selected_email']:
            raise ToolError('Draft is not present in the accessible conversation.')
        draft = drafts[0]
        if any(not isinstance(draft.get(field), str) or not draft[field].strip()
               for field in ('subject', 'body')):
            raise ToolError('Draft subject and body must not be empty. Update the draft before sending.')
        try:
            _post_json(f'/proxy/approve_message/{draft_id}',
                       {'client_id': conversation['client_id']}, 'sent')
        except ToolError as exc:
            raise ToolError(f'Sending draft {draft_id} was not confirmed. '
                            f'Check CRM before retrying; the email may already have been sent. {exc}') from None
    return {'status': 'sent', 'draft_id': draft_id,
            'mailbox_user_id': conversation['mailbox_user_id'],
            'client_id': conversation['client_id'], 'email': conversation['selected_email'],
            'sent': True}


from calendar_tools import register_calendar_tools

globals().update(register_calendar_tools(mcp, get_json, _post_json, _get_conversation, _validate_page, _page))


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Leadprogress MCP server')
    parser.add_argument('--transport', choices=('stdio', 'streamable-http'), default='stdio')
    args = parser.parse_args()
    try:
        if args.transport == 'streamable-http':
            from http_transport import run_http
            run_http(mcp)
        else:
            mcp.run(transport='stdio')
    finally:
        close_session()
