"""Verify annotations actually published to MCP clients, without calling CRM."""
import asyncio
from pathlib import Path
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import server


class ToolAnnotationTests(unittest.TestCase):
    def test_all_tools_publish_explicit_effects_over_stdio(self):
        reads = {
            'list_clients', 'list_client_attachments', 'list_conversations',
            'get_clients_summary', 'get_conversations_summary', 'get_conversation',
            'list_draft_attachments', 'read_attachment', 'list_salesmen',
            'list_calendar_events', 'list_salesman_absences', 'get_calendar_summary',
            'get_salesman_availability', 'get_client_ai_context',
        }
        # readOnlyHint, destructiveHint, idempotentHint, openWorldHint
        expected = {name: (True, False, True, False) for name in reads}
        expected.update({
            'create_reply_draft': (False, False, False, False),
            'update_reply_draft': (False, True, False, False),
            'send_reply_draft': (False, True, False, True),
            'create_calendar_event': (False, False, False, True),
            'reschedule_calendar_event': (False, True, False, True),
            'record_call_result': (False, True, False, False),
        })

        async def run():
            params = StdioServerParameters(
                command=sys.executable, args=[str(Path(server.__file__).resolve())])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = (await session.list_tools()).tools
                    self.assertEqual({tool.name for tool in tools}, set(expected))
                    for tool in tools:
                        with self.subTest(tool=tool.name):
                            self.assertIsNotNone(tool.annotations)
                            fields = ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint')
                            published = tool.annotations.model_dump(exclude_unset=True)
                            self.assertTrue(set(fields) <= published.keys())
                            self.assertEqual(tuple(published[key] for key in fields), expected[tool.name])

        asyncio.run(asyncio.wait_for(run(), 30))
