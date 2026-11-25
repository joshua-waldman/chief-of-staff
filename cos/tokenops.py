"""Personal USD budget from the authenticated TokenOps current-user endpoint.

Login is interactive. Only portal cookies enter the OS credential store; no
Chrome profile, SSO credentials, raw response or arbitrary user query is imported.
"""
import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import httpx
from . import config

PORTAL = 'https://llm.amd.com/dashboard/tokenops/my-usage'
SUMMARY = 'https://llm.amd.com/dashboard/my-budget/api/summary'
SERVICE = 'AMD-ChiefOfStaff-TokenOps'


class SignInRequired(Exception):
    pass


def number(value, *, negative=False):
    if type(value) not in (int, float) or not math.isfinite(value) or (value < 0 and not negative):
        raise ValueError('TokenOps returned an invalid budget amount.')
    return value


def normalize(data):
    if not isinstance(data, dict) or data.get('show_financial_detail') is not True:
        raise ValueError('Personal financial details are not available from TokenOps.')
    if data.get('data_error'):
        raise ValueError('TokenOps reports incomplete budget data.')
    budget = data.get('budget_summary')
    if not isinstance(budget, dict) or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', str(data.get('month', ''))):
        raise ValueError('TokenOps returned an invalid budget report.')
    freshness = budget.get('data_freshness_at') or data.get('data_freshness_at')
    if freshness is not None:
        stamp = datetime.fromisoformat(freshness.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('TokenOps returned an invalid report timestamp.')
        freshness = stamp.astimezone(timezone.utc).isoformat()
    return {'month': data['month'], 'currency': 'USD',
            'budget_usd': number(budget.get('monthly_budget_usd')),
            'spend_usd': number(budget.get('monthly_spend_usd')),
            'remaining_usd': number(budget.get('remaining_budget_usd'), negative=True),
            'source_updated_at': freshness,
            'source_stale': data.get('any_source_stale') is True,
            'coverage_pending': data.get('coverage_pending') is True,
            'fetched_at': datetime.now(timezone.utc).isoformat()}


class TokenOps:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.generation = 0
        self.connecting = False
        self.refreshing = False
        self.next_refresh = 0
        self.result = {'state': 'loading', 'report': None, 'stale': False, 'error': ''}

    @staticmethod
    def account():
        return hashlib.sha256(str(config.ROOT).encode()).hexdigest()

    @staticmethod
    def credential_store():
        import keyring
        backend = keyring.get_keyring()
        if 'plaintext' in type(backend).__name__.lower() or backend.priority <= 0:
            raise ValueError('TokenOps requires a secure OS credential store.')
        return keyring

    def credentials(self):
        vault, account = self.credential_store(), self.account()
        value = vault.get_password(SERVICE, account)
        if not value:
            raise SignInRequired()
        cookies = json.loads(value)
        parts = self._parts(cookies)
        if parts:
            values = [vault.get_password(SERVICE, account + ':' + part) for part in parts]
            if any(not isinstance(part, str) or len(part) > 800 for part in values):
                raise SignInRequired()
            cookies = json.loads(''.join(values))
        if not isinstance(cookies, dict) or not cookies or len(cookies) > 100:
            raise SignInRequired()
        if any(not isinstance(k, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k)
               or not isinstance(v, str) or len(v) > 16384 for k, v in cookies.items()):
            raise SignInRequired()
        return cookies

    def save(self, cookies):
        # WinVault has a small per-entry blob limit. Publish a manifest only after
        # every encrypted chunk has been saved; a failed save preserves old login.
        vault, account = self.credential_store(), self.account()
        previous = vault.get_password(SERVICE, account)
        text = json.dumps(cookies, separators=(',', ':'), ensure_ascii=True)
        chunks = [text[i:i+800] for i in range(0, len(text), 800)]
        if not chunks or len(chunks) > 128:
            raise ValueError('TokenOps session exceeds secure storage limits.')
        manifest = {'version': 1, 'id': uuid.uuid4().hex, 'count': len(chunks)}
        written = []
        try:
            for part, value in zip(self._parts(manifest), chunks):
                vault.set_password(SERVICE, account + ':' + part, value)
                written.append(part)
            vault.set_password(SERVICE, account, json.dumps(manifest))
        except Exception:
            self._delete_parts(vault, account, written)
            raise
        if previous:
            self._delete_parts(vault, account, self._parts(previous))

    @staticmethod
    def _parts(manifest):
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except ValueError:
                return []
        if (isinstance(manifest, dict) and manifest.get('version') == 1
                and re.fullmatch(r'[0-9a-f]{32}', str(manifest.get('id', '')))
                and type(manifest.get('count')) is int and 1 <= manifest['count'] <= 128):
            return [manifest['id'] + ':' + str(i) for i in range(manifest['count'])]
        return []

    @staticmethod
    def _delete_parts(vault, account, parts):
        for part in parts:
            try:
                vault.delete_password(SERVICE, account + ':' + part)
            except Exception:
                logging.getLogger(__name__).warning('TokenOps obsolete encrypted session chunk could not be removed.')

    def snapshot(self, refresh=False):
        with self.lock:
            report = self.result.get('report')
            if report and report['month'] != datetime.now(timezone.utc).strftime('%Y-%m'):
                self.result = {'state': 'loading', 'report': None, 'stale': False, 'error': ''}
                self.next_refresh = 0
            if not self.connecting and not self.refreshing and (refresh or time.time() >= self.next_refresh):
                self.refreshing = True
                try:
                    threading.Thread(target=self._refresh, args=(self.generation,),
                                     daemon=True, name='cos-tokenops-refresh').start()
                except Exception:
                    self.refreshing = False
                    self.next_refresh = time.time() + 60
                    self.result = {**self.result, 'error': 'TokenOps refresh could not start.'}
            return copy.deepcopy({**self.result, 'connecting': self.connecting,
                                  'refreshing': self.refreshing, 'portal_url': PORTAL})

    def _publish(self, generation, *, report=None, error='', state='ready', retry=300):
        with self.lock:
            if generation != self.generation:
                return
            if report and report['month'] != datetime.now(timezone.utc).strftime('%Y-%m'):
                report, state, error, retry = None, 'unavailable', 'TokenOps has not reported the current monthly budget yet.', 60
            prior = self.result.get('report')
            self.result = {'state': state, 'report': report or prior,
                           'stale': bool(prior and report is None), 'error': error}
            self.refreshing = False
            self.next_refresh = time.time() + retry

    def _refresh(self, generation):
        try:
            cookies = self.credentials()
            response = httpx.get(SUMMARY, params={'presentation': 'my-usage/v1'},
                                 cookies=cookies, headers={'Accept': 'application/json'},
                                 timeout=30, follow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308, 401, 403}:
                raise SignInRequired()
            if response.status_code != 200:
                raise ValueError('TokenOps request failed.')
            report = normalize(response.json())
            # Only accept refreshed cookies scoped to the exact portal host.
            renewed = {c.name: c.value for c in response.cookies.jar
                       if c.domain.lstrip('.') == 'llm.amd.com' and c.name in cookies}
            if renewed:
                with self.lock:
                    if generation != self.generation:
                        return
                    self.save({**cookies, **renewed})
            self._publish(generation, report=report)
        except SignInRequired:
            self._publish(generation, state='sign_in_required',
                          error='Connect TokenOps to read your personal budget.', retry=float('inf'))
        except Exception as error:
            logging.getLogger(__name__).warning('TokenOps refresh failed (%s); credentials and response withheld.', type(error).__name__)
            self._publish(generation, state='unavailable',
                          error='Unable to refresh TokenOps. Check portal access and AMD network/VPN.', retry=60)

    def connect(self):
        self.credential_store()  # Fail before opening a browser if storage is unsafe.
        with self.lock:
            if self.connecting:
                return self.snapshot()
            self.generation += 1
            self.cancel = threading.Event()
            self.connecting = True
            self.refreshing = False
            self.result = {'state': 'connecting', 'report': None, 'stale': False, 'error': ''}
            try:
                threading.Thread(target=self._login, args=(self.generation, self.cancel),
                                 daemon=True, name='cos-tokenops-signin').start()
            except Exception:
                self.connecting = False
                self.result = {**self.result, 'state': 'unavailable', 'error': 'TokenOps sign-in could not start.'}
                self.next_refresh = float('inf')
            return self.snapshot()

    def _login(self, generation, cancel):
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
            from urllib.parse import urlsplit
            with TemporaryDirectory(prefix='cos-tokenops-') as profile, sync_playwright() as p:
                browser = p.chromium.launch_persistent_context(profile, channel='msedge',
                            headless=False, accept_downloads=False, service_workers='block')
                try:
                    page = browser.pages[0] if browser.pages else browser.new_page()
                    try:
                        page.goto(PORTAL, wait_until='domcontentloaded', timeout=60000)
                    except BrowserTimeout:
                        pass  # The owner can finish a slow interactive sign-in.
                    deadline = time.monotonic() + 600
                    while not cancel.is_set() and time.monotonic() < deadline:
                        if page.is_closed():
                            raise SignInRequired()
                        current = urlsplit(page.url)
                        if current.scheme == 'https' and current.hostname == 'llm.amd.com' and current.path.startswith('/dashboard/'):
                            response = page.request.get(SUMMARY, params={'presentation': 'my-usage/v1'},
                                                        timeout=30000, max_redirects=0)
                            if response.status == 200:
                                report = normalize(response.json())
                                cookies = {c['name']: c['value'] for c in browser.cookies([SUMMARY])
                                           if c['domain'].lstrip('.') == 'llm.amd.com'}
                                if not cookies:
                                    raise SignInRequired()
                                with self.lock:
                                    if generation != self.generation or cancel.is_set():
                                        return
                                    self.save(cookies)
                                    self.connecting = False
                                    self._publish(generation, report=report)
                                return
                        page.wait_for_timeout(1500)
                    raise SignInRequired()
                finally:
                    browser.close()
        except Exception as error:
            logging.getLogger(__name__).warning('TokenOps sign-in failed (%s); credentials and response withheld.', type(error).__name__)
            with self.lock:
                if generation == self.generation:
                    self.connecting = False
            self._publish(generation, state='sign_in_required',
                          error='TokenOps sign-in did not finish. Reconnect and complete AMD sign-in in the browser.',
                          retry=float('inf'))

    def disconnect(self):
        with self.lock:
            self.cancel.set()
            self.generation += 1
            self.connecting = self.refreshing = False
            vault = self.credential_store()
            previous = vault.get_password(SERVICE, self.account())
            if previous:
                vault.delete_password(SERVICE, self.account())
                self._delete_parts(vault, self.account(), self._parts(previous))
            self.result = {'state': 'not_connected', 'report': None, 'stale': False, 'error': ''}
            self.next_refresh = float('inf')
            return self.snapshot()

    def close(self):
        with self.lock:
            self.cancel.set()
            self.generation += 1
            self.connecting = self.refreshing = False
            self.next_refresh = 0


_connections = {}
_connections_lock = threading.Lock()


def connection():
    """The UI and automatic routing share one account-scoped, nonblocking cache."""
    with _connections_lock:
        root = str(config.ROOT)
        if root not in _connections:
            _connections[root] = TokenOps()
        return _connections[root]
