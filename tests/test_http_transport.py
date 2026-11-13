import os
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from http_transport import create_http_app


TOKEN = 'test-only-token-' + 'x' * 32


class HTTPTransportTests(unittest.TestCase):
    def client(self):
        with patch.dict(os.environ, {'MCP_AUTH_TOKEN': TOKEN,
                                    'MCP_PUBLIC_URL': 'https://mcp.leadprogressai.app'}):
            mcp = FastMCP('test')

            @mcp.tool()
            def echo(value: str) -> str:
                return value

            return TestClient(create_http_app(mcp), base_url='https://mcp.leadprogressai.app')

    def headers(self):
        return {'Authorization': f'Bearer {TOKEN}',
                'Accept': 'application/json, text/event-stream'}

    def test_unauthorized_all_methods(self):
        with self.client() as client:
            for method in ('GET', 'POST', 'DELETE'):
                for authorization in (None, 'Bearer wrong', f'Basic {TOKEN}'):
                    headers = {} if authorization is None else {'Authorization': authorization}
                    response = client.request(method, '/mcp', headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers['www-authenticate'], 'Bearer')

    def test_initialize_list_and_call(self):
        with self.client() as client:
            init = client.post('/mcp', headers=self.headers(), json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                           'clientInfo': {'name': 'test', 'version': '1'}},
            })
            self.assertEqual(init.status_code, 200, init.text)
            headers = {**self.headers(), 'MCP-Protocol-Version': init.json()['result']['protocolVersion']}
            listed = client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json()['result']['tools'][0]['name'], 'echo')
            result = client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                'params': {'name': 'echo', 'arguments': {'value': 'ok'}}})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['result']['content'][0]['text'], 'ok')

    def test_host_and_origin_validation(self):
        with self.client() as client:
            payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
            for extra, status in (({'Host': 'evil.example'}, 421),
                                  ({'Origin': 'https://evil.example'}, 403)):
                result = client.post('/mcp', headers={**self.headers(), **extra}, json=payload)
                self.assertEqual(result.status_code, status)

    def test_missing_and_short_token_fail_startup(self):
        for token in ('', 'short', 'x' * 32 + '\n'):
            with patch.dict(os.environ, {'MCP_AUTH_TOKEN': token}):
                with self.assertRaisesRegex(ValueError, 'MCP_AUTH_TOKEN'):
                    create_http_app(FastMCP('test'))

    def test_invalid_public_url_fails_startup(self):
        with patch.dict(os.environ, {'MCP_AUTH_TOKEN': TOKEN, 'MCP_PUBLIC_URL': 'http://example.com'}):
            with self.assertRaisesRegex(ValueError, 'MCP_PUBLIC_URL'):
                create_http_app(FastMCP('test'))


if __name__ == '__main__':
    unittest.main()
