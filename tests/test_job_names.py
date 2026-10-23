from cos.job_names import compact_name, display_name
from cos.briefs import update_brief
from cos.db import Store


def test_compact_names_remove_source_ids_and_bound_length():
    assert compact_name('Release review Meeting ID: 12345 Message ID: 99999') == 'Release review'
    assert compact_name('RE: Fwd: Release readiness') == 'Release readiness'
    name = compact_name('Review release readiness and prepare a comprehensive report with all evidence')
    assert len(name.split()) <= 7 and len(name) <= 60
    assert '1234567890123' not in compact_name('Launch 1234567890123')
    assert compact_name('Review https://example.com/a') == 'Review'


def test_legacy_names_and_generated_names():
    job = {'org':'Communications', 'title':'Teams conversation: 19:meeting_long_identifier@thread.v2'}
    assert display_name(job, {}) == 'Conversation review'
    assert display_name(job, {'context':'Launch readiness and follow-up'}) == 'Launch readiness and follow-up'
    assert display_name(job, {'job_name':'Quarterly budget approval','email_subject':'A very long original subject'}) == 'Quarterly budget approval'


def test_queue_uses_short_name_without_changing_detail(tmp_path, monkeypatch):
    from cos import server
    store = Store(tmp_path/'names.db')
    monkeypatch.setattr(server, 'store', store)
    job = store.create_job('Please review the launch readiness and prepare a detailed report', 'Technology')
    update_brief(store, job, {'job_name':'Launch readiness Meeting ID: 12345'})
    row = server.jobs()[0]
    assert row['display_name'] == 'Launch readiness'
    assert store.job(job['id'])['request'] == job['request']
    assert store.job(job['id'])['title'] == job['title']
