import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from attachments import AttachmentError, extract_attachment
import server
from tests.test_server import FakePanel, ThreadingHTTPServer


def office_file(kind, text='Zażółć gęślą jaźń'):
    output = BytesIO()
    if kind == 'docx':
        name = 'word/document.xml'
        xml = f'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        </w:body></w:document>'''
    else:
        name = 'content.xml'
        xml = f'''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
          xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
          xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
        <office:body><office:text><text:h>{text}</text:h>
        <table:table><table:table-row><table:table-cell><text:p>Table<text:s text:c="2"/>cell</text:p>
        </table:table-cell></table:table-row></table:table></office:text></office:body></office:document-content>'''
    with ZipFile(output, 'w') as archive:
        archive.writestr(name, xml.encode())
    return output.getvalue()


def pdf_file(text=True, password=None, pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),
                NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'):font})})
            content = DecodedStreamObject()
            content.set_data(b'BT /F1 12 Tf 10 200 Td (PDF attachment text) Tj ET')
            page[NameObject('/Contents')] = content
    if password:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ExtractionTests(unittest.TestCase):
    def test_office_formats_and_tables(self):
        for kind in ('odt', 'docx'):
            with self.subTest(kind=kind):
                result = extract_attachment('test.' + kind.upper(), office_file(kind))
                self.assertIn('Zażółć gęślą jaźń', result['text'])
                self.assertIn('Table', result['text'])
                self.assertIn('cell', result['text'])
                self.assertFalse(result['truncated'])
                self.assertEqual(result['status'], 'ok')

    def test_pdf_text(self):
        result = extract_attachment('test.pdf', pdf_file())
        self.assertIn('PDF attachment text', result['text'])
        self.assertEqual(result['page_count'], 1)
        self.assertEqual(result['pages_read'], 1)

    def test_no_pdf_text(self):
        result = extract_attachment('scan.pdf', pdf_file(text=False))
        self.assertEqual(result['status'], 'no_text')
        self.assertEqual(result['text'], '')
        self.assertEqual(result['pages_without_text'], [1])
        self.assertTrue(any('OCR' in x for x in result['warnings']))

    def test_truncation_for_all_formats(self):
        for kind, data in (('odt',office_file('odt')), ('docx',office_file('docx')), ('pdf',pdf_file())):
            result = extract_attachment('test.' + kind, data, max_chars=12)
            self.assertTrue(result['truncated'])
            self.assertLessEqual(len(result['text']), 12)

    def test_pdf_page_limit(self):
        with patch('attachments.MAX_PDF_PAGES', 1):
            result = extract_attachment('test.pdf', pdf_file(pages=2))
        self.assertEqual(result['pages_read'], 1)
        self.assertEqual(result['page_count'], 2)
        self.assertTrue(result['truncated'])

    def test_reject_encrypted_corrupt_unsupported_and_large_files(self):
        for filename, data in (('test.pdf',pdf_file(password='secret')), ('test.pdf',b'<html>login</html>'),
                               ('test.docx',b'bad zip'), ('test.odt',b'bad zip'), ('test.exe',b'bad')):
            with self.subTest(filename=filename), self.assertRaises(AttachmentError):
                extract_attachment(filename,data)
        with patch('attachments.MAX_FILE_BYTES',1), self.assertRaises(AttachmentError):
            extract_attachment('test.pdf',pdf_file())
        with patch('attachments.MAX_XML_BYTES',1), self.assertRaises(AttachmentError):
            extract_attachment('test.odt',office_file('odt'))

    def test_xml_entities_rejected(self):
        output = BytesIO()
        with ZipFile(output,'w') as archive:
            archive.writestr('content.xml', '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///secret">]><x>&secret;</x>')
        with self.assertRaises(AttachmentError):
            extract_attachment('test.odt',output.getvalue())


class DownloadTests(unittest.TestCase):
    def tearDown(self):
        server.close_session()

    def test_size_limit_without_trusting_metadata_or_header(self):
        def handle(request):
            return httpx.Response(200,content=b'123456789')
        server._client = httpx.Client(base_url='https://panel.example',transport=httpx.MockTransport(handle))
        with patch('server.MAX_FILE_BYTES',4), self.assertRaises(ToolError):
            server._download_attachment(1)

    def test_chunked_size_limit(self):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'123'
                yield b'456'
        server._client = httpx.Client(base_url='https://panel.example',
            transport=httpx.MockTransport(lambda r: httpx.Response(200,stream=Stream())))
        with patch('server.MAX_FILE_BYTES',4), self.assertRaises(ToolError):
            server._download_attachment(1)

    def test_expired_session_is_retried_once(self):
        expired = httpx.Client(base_url='https://panel.example',transport=httpx.MockTransport(
            lambda r: httpx.Response(302,headers={'Location':'/login'})))
        fresh = httpx.Client(base_url='https://panel.example',transport=httpx.MockTransport(
            lambda r: httpx.Response(200,content=b'file')))
        try:
            with patch('server.login',side_effect=[expired,fresh]) as login, patch('server.close_session'):
                self.assertEqual(server._download_attachment(1),b'file')
                self.assertEqual(login.call_count,2)
            with patch('server.login',return_value=expired) as login, patch('server.close_session'):
                with self.assertRaises(ToolError):
                    server._download_attachment(1)
                self.assertEqual(login.call_count,2)
        finally:
            expired.close()
            fresh.close()

    def test_errors_and_redirects_not_followed(self):
        for status in (403,404,500,302):
            with self.subTest(status=status):
                server.close_session()
                calls=[]
                def handle(request):
                    calls.append(request.url)
                    return httpx.Response(status,headers={'Location':'https://other.example/file'})
                server._client = httpx.Client(base_url='https://panel.example',transport=httpx.MockTransport(handle))
                with self.assertRaises(ToolError):
                    server._download_attachment(1)
                self.assertEqual(len(calls),1)

    def test_missing_attachment_and_unsupported_format_never_download(self):
        for attachments in ([],[{'id':1,'filename':'test.exe'}]):
            with patch('server._get_conversation',return_value={'timeline':[{'payload':{'attachments':attachments}}]}), \
                 patch('server._download_attachment') as download:
                with self.assertRaises(ToolError):
                    server.read_attachment(394,'test@example.com',1)
                download.assert_not_called()


class AttachmentPanel(FakePanel):
    files = {}
    def do_GET(self):
        self.requests.append(('GET',self.path))
        path = urlsplit(self.path).path
        if self.headers.get('Cookie') != 'session=test':
            self.send_response(401)
            self.end_headers()
            return
        if path == '/crm/partial/chat':
            data={'status':'ok','mailbox_user_id':394,'client_id':394,'active_client_id':394,
                  'selected_email':'test@example.com','selected_thread_id':None,
                  'timeline':[{'type':'message','payload':{'attachments':[
                      {'id':key,'filename':name,'size':len(body)} for key,(name,body) in self.files.items()]}}],
                  'pending_approvals':[],'pending_questions':[],'threads_for_selected_email':[]}
            body=json.dumps(data).encode()
        else:
            file_id=int(path.split('/')[-2])
            body=self.files[file_id][1]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


class AttachmentMCPTest(unittest.TestCase):
    def test_three_formats_through_stdio_mcp_and_authorized_download(self):
        AttachmentPanel.files={1:('test.odt',office_file('odt')),2:('test.docx',office_file('docx')),
                               3:('test.pdf',pdf_file())}
        AttachmentPanel.requests=[]
        panel=ThreadingHTTPServer(('127.0.0.1',0),AttachmentPanel)
        thread=threading.Thread(target=panel.serve_forever,daemon=True)
        thread.start()
        async def run():
            params=StdioServerParameters(command=sys.executable,args=[str(Path(server.__file__).resolve())],
                env={'LEADPROGRESS_URL':f'http://127.0.0.1:{panel.server_port}',
                     'LEADPROGRESS_USERNAME':'operator','LEADPROGRESS_PASSWORD':'secret'})
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as client:
                    await client.initialize()
                    for key,kind in ((1,'odt'),(2,'docx'),(3,'pdf')):
                        result=await client.call_tool('read_attachment',
                            {'client_id':394,'email':'test@example.com','attachment_id':key})
                        self.assertFalse(result.isError,str(result.content))
                        data=result.structuredContent or json.loads(result.content[0].text)
                        self.assertEqual(data['format'],kind)
                        self.assertEqual(data['status'],'ok')
                        self.assertIn('PDF attachment text' if kind=='pdf' else 'Zażółć',data['text'])
                    result=await client.call_tool('read_attachment',
                        {'client_id':394,'email':'test@example.com','attachment_id':999})
                    self.assertTrue(result.isError)
        try:
            asyncio.run(asyncio.wait_for(run(),30))
            self.assertEqual(AttachmentPanel.requests.count(('POST','/login')),1)
            downloads=[p for method,p in AttachmentPanel.requests if p.startswith('/proxy/')]
            self.assertEqual(downloads,[f'/proxy/attachment/{i}/download' for i in (1,2,3)])
        finally:
            panel.shutdown()
            panel.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
