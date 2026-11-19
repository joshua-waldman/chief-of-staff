"""Public-web defaults and destination boundaries apply to every transport."""
import json
from types import SimpleNamespace
from contextlib import nullcontext
from pathlib import Path

import httpx
import pytest

from cos import browser, config, research
from cos.db import Store


@pytest.fixture
def network(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(research.socket, 'getaddrinfo',
                        lambda *a: [(2, 1, 6, '', ('93.184.215.14', 443))])
    return tmp_path


def test_existing_config_gets_public_default_and_preserves_hosts(network):
    (network / 'config.local.json').write_text(json.dumps({'research_hosts': ['amd.com']}))
    assert config.settings()['research_access'] == 'public'
    assert config.settings()['research_hosts'] == ['amd.com']
    research.validate_url('https://docs.example.org/guide?version=latest#install')
    saved = config.save_settings({'research_access': 'restricted'})
    assert saved['research_hosts'] == ['amd.com']
    with pytest.raises(ValueError, match='exact research host'):
        research.validate_url('https://docs.example.org/')
    research.validate_url('https://amd.com/docs?version=latest')
    with pytest.raises(ValueError, match='Research access'):
        config.save_settings({'research_access': 'anything'})


@pytest.mark.parametrize('url', [
    'http://example.org/', 'https://user:pass@example.org/',
    'https://example.org:8443/', 'file:///tmp/example',
])
def test_public_mode_still_rejects_unsafe_urls(network, url):
    with pytest.raises(ValueError):
        research.validate_url(url)


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fe80::1'])
def test_public_mode_rejects_private_dns(network, monkeypatch, address):
    monkeypatch.setattr(research.socket, 'getaddrinfo',
                        lambda *a: [(2, 1, 6, '', (address, 443))])
    with pytest.raises(ValueError, match='Private network'):
        research.validate_url('https://example.org/')


def test_public_redirect_to_new_host_with_query(network, monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs['follow_redirects'] is False
        if len(calls) == 1:
            return httpx.Response(302, headers={'location': 'https://docs.example.org/?lang=en'}, request=httpx.Request('GET', url))
        return httpx.Response(200, text='Public evidence', request=httpx.Request('GET', url))
    monkeypatch.setattr(research.httpx, 'get', get)
    assert research.fetch_url('https://example.org/')['source'] == 'https://docs.example.org/?lang=en'
    assert len(calls) == 2


@pytest.mark.parametrize('mode', ['public', 'restricted'])
def test_browser_cdn_private_resources_and_profile_isolation(network, monkeypatch, mode):
    from playwright import sync_api
    from cos import tools
    monkeypatch.setattr(tools, 'DATA', network / 'data')
    config.save_settings({'research_access': mode, 'research_hosts': ['example.org']})
    monkeypatch.setattr(research.socket, 'getaddrinfo', lambda host, *a:
                        [(2, 1, 6, '', ('127.0.0.1' if host == 'private.example.org' else '93.184.215.14', 443))])
    attempts = []
    profiles = []
    class Page:
        url = 'https://example.org/'
        def goto(self, *a, **kw):
            for url in ['https://cdn.example.org/script.js?v=1', 'https://private.example.org/pixel', 'http://example.org/asset', 'https://example.org/page?x=1']:
                outcomes = []
                request = SimpleNamespace(url=url, method='GET')
                context.guard(SimpleNamespace(request=request, abort=lambda: outcomes.append(False), continue_=lambda: outcomes.append(True)))
                attempts.append(outcomes[0])
            return SimpleNamespace(status=200)
        def locator(self, *a): return SimpleNamespace(inner_text=lambda **kw: 'Evidence')
        def screenshot(self, path, **kw): Path(path).write_bytes(b'example')
    class Context:
        pages = [Page()]
        def route(self, pattern, guard): self.guard = guard
        def route_web_socket(self, *a): pass
        def close(self): pass
    context = Context()
    def launch(profile, **kwargs):
        assert kwargs['service_workers'] == 'block'
        assert Path(profile).is_dir()
        profiles.append(profile)
        return context
    monkeypatch.setattr(sync_api, 'sync_playwright', lambda: nullcontext(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))))
    store = Store(network / 'test.db')
    job = store.create_job('Research', 'Technology')
    for _ in range(2):
        browser.browser_action(store, job, 'browser_read', {'url': 'https://example.org/'})
    assert attempts == [mode == 'public', False, False, True] * 2
    assert profiles[0] != profiles[1]
    assert all(not Path(profile).exists() for profile in profiles)
