"""Bounded, plain-text extraction of CRM attachments. Never executes document content."""

from io import BytesIO
from pathlib import PurePath
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring
from pypdf import PdfReader

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_XML_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 100
MAX_PDF_STREAM_BYTES = 10 * 1024 * 1024
SUPPORTED_FORMATS = {'odt', 'docx', 'pdf'}
WORD = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
TEXT = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
OFFICE = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
TABLE = '{urn:oasis:names:tc:opendocument:xmlns:table:1.0}'


class AttachmentError(Exception):
    pass


class _TextLimit(Exception):
    pass


class _Text:
    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.length = 0
        self.truncated = False

    def add(self, value):
        value = value or ''
        remaining = self.limit - self.length
        self.parts.append(value[:remaining])
        self.length += min(len(value), remaining)
        if len(value) > remaining:
            self.truncated = True
            raise _TextLimit


def file_format(filename):
    kind = PurePath(filename).suffix.lower().lstrip('.')
    if kind not in SUPPORTED_FORMATS:
        raise AttachmentError('Supported attachment formats: ODT, PDF and DOCX.')
    return kind


def _walk_xml(node, output, kind, depth=0):
    if depth > 100:
        raise AttachmentError('Document XML is too deeply nested.')
    if kind == 'docx':
        if node.tag == WORD + 't':
            output.add(node.text)
        elif node.tag == WORD + 'tab':
            output.add('\t')
        elif node.tag in (WORD + 'br', WORD + 'cr'):
            output.add('\n')
    else:
        if node.tag == TEXT + 's':
            count = int(node.get(TEXT + 'c', '1'))
            if count < 0 or count > MAX_XML_BYTES:
                raise AttachmentError('Invalid ODT whitespace count.')
            output.add(' ' * min(count, output.limit + 1))
        elif node.tag == TEXT + 'tab':
            output.add('\t')
        elif node.tag == TEXT + 'line-break':
            output.add('\n')
        else:
            output.add(node.text)
    for child in node:
        _walk_xml(child, output, kind, depth + 1)
        if kind == 'odt':
            output.add(child.tail)
    if node.tag in (WORD + 'p', WORD + 'tr', TEXT + 'p', TEXT + 'h', TABLE + 'table-row'):
        output.add('\n')
    elif node.tag in (WORD + 'tc', TABLE + 'table-cell'):
        output.add('\t')


def _read_office(data, kind, output):
    with ZipFile(BytesIO(data)) as archive:
        if len(archive.infolist()) > 2000 or sum(x.file_size for x in archive.infolist()) > MAX_XML_BYTES:
            raise AttachmentError('Unpacked document exceeds the 20 MiB limit.')
        name = 'word/document.xml' if kind == 'docx' else 'content.xml'
        with archive.open(name) as part:
            xml = part.read(MAX_XML_BYTES + 1)
        if len(xml) > MAX_XML_BYTES:
            raise AttachmentError('Document XML exceeds the size limit.')
        root = fromstring(xml, forbid_dtd=True)
        body = root.find(WORD + 'body') if kind == 'docx' else root.find('.//' + OFFICE + 'text')
        if body is None:
            raise AttachmentError('The file does not contain the expected document body.')
        _walk_xml(body, output, kind)


def extract_attachment(filename, data, max_chars=30000):
    kind = file_format(filename)
    if not 1 <= max_chars <= 100000:
        raise AttachmentError('max_chars must be between 1 and 100000.')
    if len(data) > MAX_FILE_BYTES:
        raise AttachmentError('Attachment exceeds the 10 MiB limit.')
    output = _Text(max_chars)
    warnings = ['Plain-text extraction only; images and visual layout are not included.']
    details = {}
    try:
        if kind == 'pdf':
            if not data.lstrip().startswith(b'%PDF-'):
                raise AttachmentError('The attachment is not a PDF file.')
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(''):
                raise AttachmentError('Password-protected PDF files are not supported.')
            details.update(page_count=len(reader.pages), pages_read=0, pages_without_text=[])
            for index, page in enumerate(reader.pages):
                if index >= MAX_PDF_PAGES:
                    output.truncated = True
                    warnings.append('Only the first 100 PDF pages were read.')
                    break
                content = page.get_contents()
                if content is not None and len(content.get_data()) > MAX_PDF_STREAM_BYTES:
                    raise AttachmentError('PDF page content exceeds the extraction size limit.')
                text = page.extract_text() or ''
                details['pages_read'] += 1
                if text.strip():
                    output.add(f'--- Page {index + 1} ---\n')
                    output.add(text)
                    output.add('\n')
                else:
                    details['pages_without_text'].append(index + 1)
        else:
            warnings.append('Main document body only; headers, footers and separate notes are not included.')
            _read_office(data, kind, output)
    except _TextLimit:
        pass
    except AttachmentError:
        raise
    except Exception:
        raise AttachmentError('Cannot read attachment: damaged, encrypted or invalid document.') from None
    if details.get('pages_without_text'):
        warnings.append('Some PDF pages contain no extractable text; they may require OCR. OCR is not included.')
    if output.truncated:
        warnings.append('Text was truncated by a character or page limit.')
    text = ''.join(output.parts).strip()
    if not text:
        warnings.append('No extractable text found. An image-only document requires OCR.')
    return {'status': 'ok' if text else 'no_text', 'format': kind, 'text': text,
            'truncated': output.truncated, 'warnings': warnings, **details}
