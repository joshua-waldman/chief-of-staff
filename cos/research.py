"""Public documentation retrieval with per-hop destination checks."""
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

import httpx
from .config import settings


def validate_url(url, *, dns=True, check_hosts=True):
    u = urlparse(url)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443):
        raise ValueError('Research requires HTTPS without credentials or custom ports.')
    config = settings()
    if check_hosts and config.get('research_access', 'public') == 'restricted' and u.hostname not in config['research_hosts']:
        hint = (' For GitHub source evidence, use repository_connections, repository_search and repository_read with the repository owner account; public host allowlisting does not control those authenticated reads.'
                if u.hostname in {'github.com','api.github.com','raw.githubusercontent.com'} else '')
        raise ValueError('Add this exact research host in Connections before retrieval.'+hint)
    if dns:
        addresses = socket.getaddrinfo(u.hostname, 443)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Private network targets are not supported by the research fetcher.')
    return u


def fetch_url(url):
    original = url
    visited = set()
    for _ in range(6):
        validate_url(url)
        if url in visited:
            raise ValueError('Research redirect loop detected.')
        visited.add(url)
        response = httpx.get(url, timeout=30, follow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get('location')
            if not location:
                raise ValueError('Research redirect has no destination.')
            url = urljoin(url, location)
            continue
        response.raise_for_status()
        return {'source': str(response.url), 'requested_url': original,
                'text': re.sub('<[^>]+>', ' ', response.text)[:60000]}
    raise ValueError('Research redirect limit exceeded.')
