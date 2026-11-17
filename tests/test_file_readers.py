import base64
import io
import zipfile
from pathlib import Path

import pytest

from cos import tools
from cos.db import Store
from cos.file_readers import extract


@pytest.fixture
def reader(tmp_path,monkeypatch):
    monkeypatch.setattr(tools,'DATA',tmp_path/'data')
    store=Store(tmp_path/'files.db')
    job=store.create_job('Analyze attached documents','Technology')
    def read(name,raw,**args):
        path=tools.workspace(job['id'])/name
        path.write_bytes(raw)
        record=tools.file_record(store,job['id'],path)
        return tools.Tools(store).execute(job,'read_file',{'path':record['file_id'],**args})
    return read


def test_text_encodings_html_rtf_and_mail(reader):
    assert reader('unicode.txt','Hello Ω'.encode('utf-16'))['text']=='Hello Ω'
    assert reader('export.csv','Name,café'.encode('cp1252'))['text']=='Name,café'
    assert reader('page.html',b'<script>ignore me</script><p>Actual finding</p>')['text']=='Actual finding'
    assert 'Hello world' in reader('memo.rtf',br'{\rtf1\ansi Hello world}')['text']
    mail=b'From: a@example.com\nSubject: Evidence\nContent-Type: text/plain; charset=utf-8\n\nActual email body'
    assert 'Actual email body' in reader('message.eml',mail)['text']


def test_pdf_text_scans_visual_pages_and_encryption(reader):
    from pypdf import PdfWriter
    writer=PdfWriter(); writer.add_blank_page(width=200,height=300)
    out=io.BytesIO();writer.write(out)
    text=reader('scan.pdf',out.getvalue())
    assert 'No extractable text' in text['text'] and 'PAGE 1' in text['text']
    visual=reader('scan.pdf',out.getvalue(),page=1)
    assert base64.b64decode(visual['image_data']).startswith(b'\x89PNG')
    assert visual['page_count']==1 and visual['page']==1
    for page in (0,True,2):
        with pytest.raises(ValueError,match='page'):
            reader('scan.pdf',out.getvalue(),page=page)
    writer.encrypt('secret');out=io.BytesIO();writer.write(out)
    with pytest.raises(ValueError,match='Password-protected'):
        reader('locked.pdf',out.getvalue())


def test_zip_members_are_readable_and_traversal_is_rejected(reader):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr('folder/report.txt','Verified evidence')
        archive.writestr('../escape.txt','Must not escape')
    raw=out.getvalue()
    assert 'listing only' in reader('bundle.zip',raw)['text']
    assert reader('bundle.zip',raw,member='folder/report.txt')['text']=='Verified evidence'
    for member in ('../escape.txt','/root.txt','C:/absolute.txt','folder\\report.txt'):
        with pytest.raises(ValueError,match='traversal'):
            reader('bundle.zip',raw,member=member)


def test_office_tables_and_spreadsheet_values(reader):
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches
    from openpyxl import Workbook
    doc=Document();doc.add_paragraph('Document evidence');doc.add_table(rows=1,cols=1).cell(0,0).text='Word table'
    out=io.BytesIO();doc.save(out)
    assert 'Word table' in reader('report.docx',out.getvalue())['text']
    deck=Presentation();slide=deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_table(1,1,0,0, Inches(3), Inches(1)).table.cell(0,0).text='Slide table'
    out=io.BytesIO();deck.save(out)
    assert 'Slide table' in reader('report.pptx',out.getvalue())['text']
    book=Workbook();book.active.append(['Metric',42]);out=io.BytesIO();book.save(out)
    assert 'Metric | 42' in reader('metrics.xlsx',out.getvalue())['text']


def test_open_document_and_binary_limits(reader):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        archive.writestr('content.xml','<root><p>Open document evidence</p></root>')
    assert 'Open document evidence' in reader('notes.odt',out.getvalue())['text']
    for name,raw in [('legacy.doc',b'\xd0\xcf\x11\xe0rest'),('program.exe',b'MZbinary'),('unknown.bin',b'\x00binary')]:
        with pytest.raises(ValueError,match='Office|Unsupported'):
            reader(name,raw)


def test_pdf_page_cannot_bypass_job_attachment_scope(reader,tmp_path):
    with pytest.raises(ValueError,match='only for PDF'):
        reader('notes.txt',b'text',page=1)


def test_visual_page_metadata_reaches_model(tmp_path):
    from cos.engine import Engine
    messages=[]
    Engine(Store(tmp_path/'images.db')).add_result(messages,'page-read',
        {'image_data':'cGl4ZWxz','mime':'image/png','page':2,'page_count':17,'instruction':'Only page 2 was inspected.'})
    assert '"page_count": 17' in messages[0]['content']
    assert 'Only page 2' in messages[0]['content']
    assert messages[1]['content'][0]['type']=='image_url'
