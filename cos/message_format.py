"""Build display-only message HTML from a small, inert vocabulary.

Never forward source attributes, CSS, media URLs, or unknown element names to
the browser. Original bodies remain available for provenance and image reads.
"""
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit


TAGS = frozenset({'p', 'div', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'del',
                  'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'table',
                  'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'hr'})
VOID = frozenset({'br', 'hr'})
HIDDEN = frozenset({'script', 'style', 'iframe', 'object', 'template', 'svg', 'math'})


def safe_link(value):
    # Reject control characters before URL parsing (which can discard them).
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return ''
    value = value.strip()
    try:
        url = urlsplit(value)
        if url.scheme.lower() in {'http', 'https'} and url.hostname and not url.username and not url.password:
            return value
        if url.scheme.lower() == 'mailto' and url.path and not url.netloc:
            return value
    except ValueError:
        pass
    return ''


class MessageHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag in HIDDEN:
                self.hidden.append(tag)
            return
        if tag in HIDDEN:
            self.hidden.append(tag)
            return
        attrs = dict(attrs)
        if tag in TAGS:
            self.parts.append('<' + tag + '>')
            if tag not in VOID:
                self.stack.append(tag)
        elif tag == 'a':
            href = safe_link(attrs.get('href', ''))
            if href:
                self.parts.append('<a href="' + escape(href, quote=True) + '" target="_blank" rel="noopener noreferrer">')
                self.stack.append(tag)
        elif tag == 'at' or 'schema.skype.com/mention' in attrs.get('itemtype', '').lower():
            self.parts.append('@')
        elif tag == 'img' or (tag == 'uriobject' and attrs.get('type', '').lower().startswith('picture')):
            self.parts.append(escape('[Teams image' + (': ' + attrs['alt'] if attrs.get('alt') else '') + ']'))
        elif tag in {'attachment', 'uriobject'}:
            self.parts.append(escape('[Teams attachment' + (': ' + attrs['title'] if attrs.get('title') else '') + ']'))

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                # Malformed hidden markup stays hidden until its outer close.
                del self.hidden[len(self.hidden) - 1 - self.hidden[::-1].index(tag):]
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append('</' + current + '>')
                if current == tag:
                    break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(escape(data))


def message_html(body):
    parser = MessageHTML()
    parser.feed(body)
    parser.close()
    return ''.join(parser.parts) + ''.join('</' + tag + '>' for tag in reversed(parser.stack))


def display_sources(sources):
    """Teams sources contain raw markup; email bodies are already plain text."""
    return [{**source, **({'body_html': message_html(source['body'])}
                           if source['source'] == 'teams' else {})} for source in sources]
