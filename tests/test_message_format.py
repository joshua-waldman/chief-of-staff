from html.parser import HTMLParser

import pytest

from cos.communication_bridge import plain
from cos.conversations import save_source, source_messages
from cos.db import Store
from cos.message_format import message_html, safe_link


def test_formatting_and_teams_references():
    body = ('<p>Hello <at id="1">Alex</at> &amp; team<br><b>Review</b> <em>today</em></p>'
            '<ul><li>First</li><li>Second</li></ul><blockquote>Earlier reply</blockquote>'
            '<table style="color:red"><tr><th>Owner</th><td>Alex</td></tr></table>'
            '<pre><code>&lt;p&gt;literal code&lt;/p&gt;</code></pre>'
            '<a href="https://example.com/?a=1&amp;b=2">Details</a>'
            '<img src="https://example.com/pixel" alt="Shared screenshot">'
            '<URIObject type="File.1" title="notes.txt"></URIObject>')
    result = message_html(body)
    assert '<p>Hello @Alex &amp; team<br><b>Review</b> <em>today</em></p>' in result
    assert '<ul><li>First</li><li>Second</li></ul>' in result
    assert '<blockquote>Earlier reply</blockquote>' in result
    assert '<table><tr><th>Owner</th><td>Alex</td></tr></table>' in result
    assert '<code>&lt;p&gt;literal code&lt;/p&gt;</code>' in result
    assert 'href="https://example.com/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer"' in result
    assert '[Teams image: Shared screenshot]' in result
    assert '[Teams attachment: notes.txt]' in result
    assert 'pixel' not in result


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'JaVaScRiPt:alert(1)',
    'java\nscript:alert(1)', 'data:text/html,hello', '//example.com', '/relative',
    'file:///example', 'https://user:secret@example.com', 'https://[invalid', ''])
def test_unsafe_links_are_not_clickable(url):
    assert not safe_link(url)


@pytest.mark.parametrize('url', ['https://example.com/path', 'http://example.com',
                                'mailto:owner@example.com'])
def test_safe_links(url):
    assert safe_link(url) == url


@pytest.mark.parametrize('body', [
    '<script>alert(1)</script><p onclick="alert(1)" style="background:url(https://example.com)">Safe</p>',
    '<svg><a href="javascript:alert(1)">hidden</a></svg><p>Safe</p>',
    '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">',
    '<a href="jav&#x61;script:alert(1)">Unsafe link</a>',
    '<iframe srcdoc="<script>alert(1)</script>">Hidden</iframe><b>Safe</b>',
    '<input autofocus onfocus=alert(1)><form action="https://example.com"><button>Submit</button></form>',
    '<a href="https://example.com/&quot; onclick=&quot;alert(1)">Safe destination</a>',
    '<!-- comment --><style>hidden</style><p>Safe</p><img src=x onerror=alert(1)>',
])
def test_output_vocabulary_excludes_active_content(body):
    from cos.message_format import TAGS

    class Inspect(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag in TAGS | {'a'}
            if tag == 'a':
                assert dict(attrs).keys() == {'href', 'target', 'rel'}
                assert safe_link(dict(attrs)['href'])
            else:
                assert not attrs

    result = message_html(body)
    Inspect().feed(result)
    assert '<script' not in result and '<img' not in result
    assert '<style' not in result and '<svg' not in result


def test_malformed_markup_and_encoded_code_remain_inert():
    assert message_html('<p><b>Unclosed') == '<p><b>Unclosed</b></p>'
    assert message_html('<b><i>Mismatch</b> end</i>') == '<b><i>Mismatch</i></b> end'
    assert message_html('&lt;script&gt;literal&lt;/script&gt;') == '&lt;script&gt;literal&lt;/script&gt;'
    assert message_html('First line\nSecond & third < 4') == 'First line\nSecond &amp; third &lt; 4'
    assert message_html('<script>Hidden') == ''


def test_existing_sources_get_display_html_without_changing_evidence(tmp_path):
    store = Store(tmp_path/'format.db')
    job = store.create_job('Review messages', 'Communications')
    raw = '<p>Saved <strong>Teams message</strong></p><img src="https://example.com/image">'
    email = 'Literal <b>email text</b> & symbols'
    with store.connect() as c:
        save_source(c, job['id'], 'teams', 'teams', 'Alex', '', '', raw)
        save_source(c, job['id'], 'email', 'email', 'Alex', '', '', email)
    displayed = {s['source']: s for s in store.detail(job['id'])['source_messages']}
    assert displayed['teams']['body_html'] == '<p>Saved <strong>Teams message</strong></p>[Teams image]'
    assert displayed['teams']['body'] == raw
    assert displayed['email']['body'] == email
    assert 'body_html' not in displayed['email']
    originals = source_messages(store, job['id'])
    assert all('body_html' not in s for s in originals)
    assert next(s['body'] for s in originals if s['source'] == 'teams') == raw


def test_email_html_is_already_converted_to_text():
    result = plain({'contentType': 'HTML', 'content': '<p>Hello &amp; team</p><br><b>Review</b>'})
    assert 'Hello & team' in result and 'Review' in result
    assert '<' not in result and '\n' in result
