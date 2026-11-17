"""Read document contents without executing files, macros, or embedded links."""
import io
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_EXPANDED = 100 * 1024 * 1024


def checked_zip(path):
    archive = zipfile.ZipFile(path)
    if len(archive.infolist()) > 10000 or sum(i.file_size for i in archive.infolist()) > MAX_EXPANDED:
        archive.close()
        raise ValueError('Expanded archive exceeds 100 MB or 10,000 entries.')
    return archive


def decode(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16').replace('\r\n','\n').replace('\r','\n')
    if b'\x00' in raw[:4096]:
        raise ValueError('Unsupported binary file. Convert it to PDF, DOCX, PPTX, XLSX, an image, or text before analysis.')
    try:
        return raw.decode('utf-8-sig').replace('\r\n','\n').replace('\r','\n')
    except UnicodeDecodeError:
        # Windows exports commonly use Windows-1252. Never silently replace bytes.
        try:
            return raw.decode('cp1252').replace('\r\n','\n').replace('\r','\n')
        except UnicodeDecodeError:
            raise ValueError('Unknown text encoding; export as UTF-8 before analysis.') from None


def extract(path):
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if raw.startswith(b'\xd0\xcf\x11\xe0'):
        raise ValueError('Legacy or Microsoft-protected Office file. Export an accessible PDF or modern Office copy; encrypted contents have not been read.')
    if suffix in {'.docx', '.pptx', '.xlsx', '.xlsm', '.odt', '.ods', '.odp', '.epub'}:
        with checked_zip(path):
            pass
    if suffix in {'.xlsx', '.xlsm'}:
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        result = ['[Spreadsheet values use cached formula results; formulas are not recalculated.]']
        try:
            for sheet in book:
                result.append('SHEET: ' + sheet.title)
                for index, row in enumerate(sheet.iter_rows(values_only=True)):
                    if index >= 2000:
                        result.append('[Worksheet truncated after 2,000 rows]')
                        break
                    result.append(' | '.join('' if c is None else str(c) for c in row))
        finally:
            book.close()
        return '\n'.join(result)
    if suffix == '.docx':
        from docx import Document
        doc = Document(path)
        return '\n'.join([p.text for p in doc.paragraphs] +
            [' | '.join(c.text for c in row.cells) for table in doc.tables for row in table.rows])
    if suffix == '.pptx':
        from pptx import Presentation
        def shapes(items):
            for shape in items:
                if shape.has_text_frame:
                    yield shape.text
                if shape.has_table:
                    yield from (' | '.join(c.text for c in row.cells) for row in shape.table.rows)
                if hasattr(shape, 'shapes'):
                    yield from shapes(shape.shapes)
        return '\n'.join('SLIDE '+str(n)+'\n'+'\n'.join(shapes(slide.shapes))
            for n, slide in enumerate(Presentation(path).slides, 1))
    if suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('Password-protected PDF; contents are unreadable without an accessible copy.')
        result = ['[PDF text extraction omits diagrams and scanned text. Use read_file with page:1 (then subsequent pages) for visual analysis.]']
        for n, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            result.append('PAGE '+str(n)+'\n'+(text if text.strip() else '[No extractable text. Read this page visually; it may be scanned or blank.]'))
        return '\n'.join(result)
    if suffix in {'.odt', '.ods', '.odp'}:
        with checked_zip(path) as archive:
            root = ET.fromstring(archive.read('content.xml'))
        return '\n'.join(''.join(node.itertext()) for node in root.iter()
            if node.tag.rsplit('}', 1)[-1] in {'p', 'h'})
    if suffix == '.epub':
        from .teams_content import text
        with checked_zip(path) as archive:
            return '\n'.join(name+'\n'+text(decode(archive.read(name))) for name in archive.namelist()
                if Path(name).suffix.lower() in {'.html', '.xhtml', '.htm'})
    if suffix == '.zip':
        with checked_zip(path) as archive:
            return '[Archive listing only. Use read_file with member:"exact/name" to read a file inside.]\n'+'\n'.join(
                f'{i.filename} ({i.file_size} bytes)' for i in archive.infolist())
    if suffix == '.eml':
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        body = mail.get_body(preferencelist=('plain', 'html'))
        content = body.get_content() if body else ''
        if body and body.get_content_subtype() == 'html':
            from .teams_content import text
            content = text(content)
        return '\n'.join(f'{h}: {mail.get(h, "")}' for h in ('From', 'To', 'Date', 'Subject'))+'\n\n'+content
    if suffix == '.rtf':
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(decode(raw))
    if suffix in {'.html', '.htm'}:
        from .teams_content import text
        return text(decode(raw))
    # Detect executable/compressed/media signatures rather than returning gibberish.
    if raw.startswith((b'MZ', b'PK\x03\x04', b'\x1f\x8b', b'Rar!', b'7z\xbc\xaf', b'ID3')) or suffix in {'.doc', '.xls', '.ppt', '.mp4', '.mp3', '.wav', '.mov', '.7z', '.rar'}:
        raise ValueError('Unsupported format '+suffix+'. Convert to a readable document, image, or text. Audio/video transcription is unavailable.')
    return decode(raw)


def pdf_page(path, page):
    import pypdfium2 as pdfium
    if type(page) is not int or page < 1:
        raise ValueError('PDF page must be a positive one-based integer.')
    with pdfium.PdfDocument(path) as document:
        count = len(document)
        if page > count:
            raise ValueError('PDF page exceeds page count '+str(count)+'.')
        pdfpage = document[page-1]
        try:
            width, height = pdfpage.get_size()
            bitmap = pdfpage.render(scale=min(2, 2000/max(width, height)))
            try:
                output = io.BytesIO()
                bitmap.to_pil().save(output, format='PNG')
                return output.getvalue(), count
            finally:
                bitmap.close()
        finally:
            pdfpage.close()
