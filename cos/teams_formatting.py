"""Small escaped-text renderer for Teams; no source HTML, links, or styles pass through."""
import re
from html import escape

MAX_HTML_BYTES = 24000


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
    def flush():
        if paragraph:
            blocks.append('<p>'+'<br>'.join(inline(line) for line in paragraph)+'</p>')
            paragraph.clear()
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if code is not None:
            if re.fullmatch(r'\s*```\s*', line):
                blocks.append('<pre><code>'+escape('\n'.join(code))+'</code></pre>')
                code = None
            else:
                code.append(line)
        elif re.fullmatch(r'\s*```[A-Za-z0-9_-]*\s*', line):
            flush()
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
