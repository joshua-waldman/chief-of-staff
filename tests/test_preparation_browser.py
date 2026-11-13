from pathlib import Path
from types import SimpleNamespace

import pytest

from cos import browser
from cos.db import Store


@pytest.mark.parametrize('action,token,blocked', [
    ('browser_read', {}, True),
    ('browser_action', {'_preparation_review': 'reviewed-read'}, True),
    ('browser_action', {'_external_change_review': 'owner-approved-write'}, False),
])
def test_navigation_blocks_mutating_transport_until_external_change_approval(tmp_path, monkeypatch, action, token, blocked):
    from cos import tools
    monkeypatch.setattr(tools, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(browser, 'settings', lambda: {'research_hosts': ['amd.com']})
    monkeypatch.setattr(browser, 'validate_url', lambda url: url)
    routes = []
    socket = SimpleNamespace(closed=False)
    socket.close = lambda: setattr(socket, 'closed', True)

    class Page:
        main_frame = object()
        url = 'https://amd.com/docs'

        def goto(self, *args, **kwargs):
            for method in ('GET', 'POST', 'PATCH', 'DELETE'):
                route = SimpleNamespace(request=SimpleNamespace(
                    method=method, url=self.url, frame=self.main_frame,
                    is_navigation_request=lambda: False), aborted=False, continued=False)
                route.abort = lambda route=route: setattr(route, 'aborted', True)
                route.continue_ = lambda route=route: setattr(route, 'continued', True)
                context.guard(route)
                routes.append(route)
            if context.socket_guard:
                context.socket_guard(socket)
            return SimpleNamespace(status=200)

        def locator(self, *args):
            return SimpleNamespace(inner_text=lambda **kwargs: 'Retrieved evidence')

        def screenshot(self, path, **kwargs):
            Path(path).write_bytes(b'fake-test-screenshot')

    class Context:
        pages = [Page()]
        socket_guard = None

        def route(self, pattern, guard):
            self.guard = guard

        def route_web_socket(self, pattern, guard):
            self.socket_guard = guard

        def close(self):
            pass

    context = Context()

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=lambda *args, **kwargs: context))

        def __exit__(self, *args):
            pass

    monkeypatch.setattr('playwright.sync_api.sync_playwright', Playwright)
    store = Store(tmp_path / 'test.db')
    job = store.create_job('Read evidence', 'Technology')
    browser.browser_action(store, job, action, {'url': 'https://amd.com/docs', **token})
    assert routes[0].continued and not routes[0].aborted
    assert all(route.aborted == blocked for route in routes[1:])
    assert socket.closed == blocked
