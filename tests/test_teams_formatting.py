from html.parser import HTMLParser

import pytest

from cos.teams_formatting import render,page_end,fragment,MAX_HTML_BYTES,status_table
from cos.task_status import short_reference,timestamp


def test_teams_html_preserves_spacing_headings_and_copyable_commands():
    result=render('COS inbox\n\n## Task status\n\n**36 active** · **19 ready for review**\n2,214 total\n\n**1. Release plan**\nYour input needed\n`!cos status abcdef12`')
    assert '<h3>Task status</h3>' in result
    assert '<strong>36 active</strong>' in result
    assert '<br>2,214 total</p>' in result
    assert '<p><strong>1. Release plan</strong><br>Your input needed<br><code>!cos status abcdef12</code></p>' in result


def test_untrusted_html_links_and_mentions_are_literal_not_active_content():
    source='<script>alert(1)</script> <img src="https://example.com/private"> <at>Owner</at> & <b>text</b>'
    result=render(source)
    assert result=='<p>&lt;script&gt;alert(1)&lt;/script&gt; &lt;img src=&quot;https://example.com/private&quot;&gt; &lt;at&gt;Owner&lt;/at&gt; &amp; &lt;b&gt;text&lt;/b&gt;</p>'
    result=render('`<img src=x>` **<script>x</script>**')
    assert '<code>&lt;img src=x&gt;</code>' in result
    assert '<strong>&lt;script&gt;x&lt;/script&gt;</strong>' in result


@pytest.mark.parametrize('text', ['<&🙂'*3000, 'line\n'*3000, '# Heading\n\n'+('é'*9000)])
def test_pagination_budgets_escaped_html_and_multibyte_characters_without_losing_source(text):
    cursor=0
    parts=[]
    footer='\n\nContinued: !cos more 0123456789abcdef'
    while cursor<len(text):
        end=page_end(text,cursor,6000,footer)
        assert cursor<end<=cursor+6000
        body=fragment(text,cursor,end)+(footer if end<len(text) else '')
        assert len(render('COS inbox\n\n'+body).encode('utf-8'))<=MAX_HTML_BYTES
        parts.append(text[cursor:end])
        cursor=end
    assert ''.join(parts)==text


def test_exact_action_json_stays_opaque_across_preview_pages():
    source='Exact arguments:\n```json\n{\n  "body": "'+('**literal** `command` <img> & '*600)+'"\n}\n```\n\nEnd of saved preview.'
    cursor=0
    bodies=[]
    while cursor<len(source):
        end=page_end(source,cursor,6000,'\n\nContinued: !cos more 0123456789abcdef')
        bodies.append(render(fragment(source,cursor,end)))
        cursor=end
    assert len(bodies)>2
    assert all('<strong>literal</strong>' not in body and '<code>command</code>' not in body for body in bodies)
    assert any('**literal** `command` &lt;img&gt; &amp;' in body for body in bodies)
    assert bodies[-1].endswith('<p>End of saved preview.</p>')


@pytest.mark.parametrize('length',[5933,5934,5935,5936,5937])
def test_page_boundaries_never_split_a_closing_code_fence(length):
    source='Exact arguments:\n```json\n{"body":"'+('x'*length)+'**literal** `command` <tag>"}\n```\n\nEnd of saved preview.'
    cursor=0
    bodies=[]
    while cursor<len(source):
        end=page_end(source,cursor,6000,'\n\nContinued: !cos more 0123456789abcdef')
        bodies.append(render(fragment(source,cursor,end)))
        cursor=end
    assert all('<strong>literal</strong>' not in body for body in bodies)
    assert bodies[-1].endswith('<p>End of saved preview.</p>')


def test_renderer_uses_only_fixed_semantic_tags_without_attributes():
    class Tags(HTMLParser):
        def handle_starttag(self,tag,attrs):
            assert tag in {'p','br','h3','strong','code','pre'}
            assert not attrs
    Tags().feed(render('## Heading\n\n**Title**\n`!cos status <name>`\n\n```json\n{"body":"<script>"}\n```'))


def test_short_commands_extend_colliding_prefixes_and_times_are_readable():
    first='abcdef120'+'0'*23
    second='abcdef121'+'1'*23
    assert short_reference(first,[first,second])=='abcdef120'
    assert short_reference(second,[first,second])=='abcdef121'
    assert timestamp('2026-11-24T16:05:09.123456-06:00')=='Nov 24, 2026 · 22:05 UTC'
    assert timestamp('2026-11-24T22:05:09')=='Nov 24, 2026 · 22:05 (timezone unknown)'
    assert timestamp('not a date')=='Not recorded'


def test_status_table_has_three_columns_and_escapes_every_cell():
    row={'job':'<script>Title</script>','context':'Context & <img src=x>','status':'Waiting\nReady for review',
         'next_step':'Ask <at>Owner</at> for "scope"','command':'!cos status abcdef12','updated':'Nov 24, 2026 · 22:05 UTC'}
    html=render(status_table([row]))
    assert html.count('<tr>')==2 and html.count('<td ')==3
    assert '<thead>' in html and '<tbody>' in html
    assert '&lt;script&gt;Title&lt;/script&gt;' in html and '&lt;img src=x&gt;' in html
    assert '<script>' not in html and '<img ' not in html and '<at>' not in html
    assert 'Waiting<br>Ready for review' in html
    assert '<code>!cos status abcdef12</code>' in html
    assert '&lt;at&gt;Owner&lt;/at&gt;' in html


def test_invalid_table_payload_stays_literal_code():
    for source in ['not JSON','{"html":"<script>"}','[{"job":"title","style":"color:red"}]']:
        html=render('```cos-table\n'+source+'\n```')
        assert '<table ' not in html and '<pre><code>' in html
        assert '<script>' not in html
