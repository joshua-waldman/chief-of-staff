"""Small escaped-text renderer for Teams; no source HTML, links, or styles pass through."""
import re
import json
from html import escape

MAX_HTML_BYTES = 24000


def status_table(rows):
    """Serialize server-owned structure; cell strings never become source HTML."""
    return '```cos-table\n'+json.dumps(rows,ensure_ascii=False,separators=(',',':'))+'\n```'


def render_status_table(source):
    try:
        rows=json.loads(source)
    except (TypeError,ValueError):
        return None
    fields={'job','context','status','next_step','command','updated'}
    if not isinstance(rows,list) or not 1<=len(rows)<=6 or any(
            not isinstance(row,dict) or set(row)!=fields or any(not isinstance(value,str) for value in row.values())
            for row in rows):
        return None
    def cell(value):
        return escape(value).replace('\n','<br>')
    result=['<table border="1" cellpadding="6" cellspacing="0" width="100%" style="border-collapse:collapse;table-layout:fixed">',
            '<thead><tr><th scope="col" width="40%">Job</th><th scope="col" width="20%">Current Status</th><th scope="col" width="40%">Next Steps</th></tr></thead><tbody>']
    for row in rows:
        result.append('<tr><td style="vertical-align:top;overflow-wrap:anywhere"><strong>'+cell(row['job'])+'</strong>'+
                      ('<br>'+cell(row['context']) if row['context'] else '')+'<br>Updated '+cell(row['updated'])+'</td>'+
                      '<td style="vertical-align:top;overflow-wrap:anywhere"><strong>'+cell(row['status'])+'</strong>'+
                      '</td>'+
                      '<td style="vertical-align:top;overflow-wrap:anywhere">'+cell(row['next_step'])+
                      '<br><code>'+cell(row['command'])+'</code></td></tr>')
    return ''.join(result)+'</tbody></table><br>'


def inline(text):
    parts, cursor = [], 0
    for match in re.finditer(r'`([^`\n]+)`|\*\*([^*\n]+)\*\*', text):
        parts.append(escape(text[cursor:match.start()]))
        tag, value = ('code', match[1]) if match[1] is not None else ('strong', match[2])
        parts.append('<'+tag+'>'+escape(value)+'</'+tag+'>')
        cursor = match.end()
    return ''.join(parts)+escape(text[cursor:])


def render(text):
    """Honor paragraphs, headings and inline emphasis with a fixed HTML tag set."""
    blocks, paragraph, code = [], [], None
    language=''
    def flush():
        if paragraph:
            blocks.append('<p>'+'<br>'.join(inline(line) for line in paragraph)+'</p>')
            paragraph.clear()
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if code is not None:
            if re.fullmatch(r'\s*```\s*', line):
                table=render_status_table('\n'.join(code)) if language=='cos-table' else None
                blocks.append(table or '<pre><code>'+escape('\n'.join(code))+'</code></pre>')
                code = None
            else:
                code.append(line)
        elif re.fullmatch(r'\s*```[A-Za-z0-9_-]*\s*', line):
            flush()
            language=line.strip()[3:]
            code = []
        elif line == 'COS inbox' and not blocks and not paragraph:
            blocks.append('<p><strong>COS inbox</strong></p>')
        elif not line.strip():
            flush()
        elif re.match(r'^#{1,6}\s+\S', line):
            flush()
            blocks.append('<h3>'+inline(re.sub(r'^#{1,6}\s+', '', line))+'</h3>')
        else:
            paragraph.append(line)
    flush()
    if code is not None:
        blocks.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>')
    return ''.join(blocks)


def fragment(text,start,end):
    def fenced(stop):
        prefix=text[:stop]
        lines=prefix.splitlines()
        if stop<len(text) and prefix and not prefix.endswith('\n'):
            lines=lines[:-1]
        opened=False
        for line in lines:
            if opened:
                if re.fullmatch(r'\s*```\s*',line):
                    opened=False
            elif re.fullmatch(r'\s*```[A-Za-z0-9_-]*\s*',line):
                opened=True
        return opened
    # Continue opaque source/code blocks on each page. Formatting inside an exact
    # action preview must never turn literal Markdown in JSON values into emphasis.
    return ('```\n' if fenced(start) else '')+text[start:end]+('\n```' if fenced(end) else '')


def page_end(text, start, limit, footer):
    """Split source text, never HTML, and budget the actual escaped UTF-8 payload."""
    end = min(len(text), start+limit)
    def fits(stop):
        suffix = footer if stop < len(text) else ''
        return len(render('COS inbox\n\n'+fragment(text,start,stop)+suffix).encode('utf-8')) <= MAX_HTML_BYTES
    if not fits(end):
        low, high = start+1, end
        while low <= high:
            middle = (low+high)//2
            if fits(middle):
                low = middle+1
            else:
                high = middle-1
        end = high
    # Prefer a paragraph boundary so headings and copyable commands stay together.
    if end < len(text):
        boundary = text.rfind('\n\n', start, end)
        if boundary > start+(end-start)*.6:
            end = boundary+2
        line_start=text.rfind('\n',0,end)+1
        line_end=text.find('\n',line_start)
        line=text[line_start:line_end if line_end>=0 else len(text)]
        if re.fullmatch(r'\s*```[A-Za-z0-9_-]*\s*',line) and line_start>start:
            end=line_start
    while end > start and not fits(end):
        end -= 1
    if end <= start:
        raise ValueError('The command prefix is too long to fit a Teams reply.')
    return end
