"""Optional, tenant-approved public-client authentication for Teams cleanup.

This cache is encrypted by the OS and separate from the installed skills.
Only mark-as-read uses this identity; Graph remains the authorization boundary.
"""
import base64
import json
import os
import time
from pathlib import Path
from uuid import UUID

from .config import settings

SCOPES = ['https://graph.microsoft.com/Chat.ReadWrite', 'https://graph.microsoft.com/User.Read']
SETUP = ('Teams mark-as-read requires delegated Chat.ReadWrite. Configure an organization-approved '
         'Entra public client with Chat.ReadWrite and User.Read in Connections, then sign in. '
         'Alternatively, mark the chat read in Teams. Repeating the shared Microsoft sign-in '
         'does not add this permission.')


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {'client_id', 'tenant_id'}:
        raise ValueError('Teams authentication accepts client_id and tenant_id only.')
    if not value:
        return
    for field in ('client_id', 'tenant_id'):
        try:
            UUID(value.get(field, ''))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Teams '+field+' must be a GUID.') from None
    if value['client_id'].lower() == '1fec8e78-bce4-4aaf-ab1b-5451cc387264':
        raise ValueError('Use your organization-approved app registration, not the shared Teams Desktop client.')


def claims(token):
    """Diagnostic hints only; decoding never authenticates a token."""
    try:
        part = token.split('.')[1]
        value = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def cache():
    from msal_extensions import PersistedTokenCache, build_encrypted_persistence
    config = settings()['teams_auth']
    validate_config(config)
    if not config:
        raise ValueError(SETUP)
    location = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.local/share'))) / 'AMD-ChiefOfStaff' / 'teams-auth'
    location.mkdir(parents=True, exist_ok=True)
    name = config['tenant_id'].lower()+'-'+config['client_id'].lower()+'.cache'
    # No plaintext fallback. PersistedTokenCache also locks cross-process updates.
    return PersistedTokenCache(build_encrypted_persistence(str(location/name)))


def client():
    import msal
    config = settings()['teams_auth']
    saved = cache()
    return msal.PublicClientApplication(config['client_id'],
        authority='https://login.microsoftonline.com/'+config['tenant_id'], token_cache=saved)


def status(graph=None):
    if settings()['teams_auth']:
        try:
            signed_in = bool(cache().find('Account'))
            return {'configured': True, 'available': signed_in,
                    'summary': 'Separate Teams sign-in saved; access is checked when used.' if signed_in else 'Sign in to the configured Teams app.'}
        except Exception:
            return {'configured': True, 'available': False,
                    'summary': 'Teams authentication is unavailable. Install requirements and check encrypted credential storage.'}
    # Inspect the loaded token without refreshing it or returning its contents.
    info = claims(getattr(graph, '_access_token', ''))
    available = 'Chat.ReadWrite' in info.get('scp', '').split() and info.get('exp', 0) > time.time()
    return {'configured': False, 'available': available,
            'summary': 'Shared Microsoft token includes Chat.ReadWrite.' if available else SETUP}


def token():
    app = client()
    accounts = app.get_accounts()
    if len(accounts) != 1:
        raise ValueError('Sign in to the Teams app in Connections before marking a chat read.')
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) or {}
    if not result.get('access_token'):
        raise ValueError('Teams sign-in needs renewal or tenant consent. Open Connections and sign in to the Teams app.')
    return result['access_token']


def sign_in():
    app = client()
    flow = app.initiate_device_flow(scopes=SCOPES)
    if 'user_code' not in flow:
        raise ValueError('Microsoft could not start Teams sign-in. Check the app registration and tenant public client policy. '+str(flow.get('error', '')))
    print(flow['message'], flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if not result.get('access_token'):
        raise ValueError('Teams sign-in failed: '+str(result.get('error', 'unknown_error'))+
                         '. Check tenant consent and public client policy.')
    # A successful account switch replaces older identities in this dedicated cache.
    owner = result.get('id_token_claims', {}).get('oid')
    if owner:
        for account in app.get_accounts():
            if account.get('local_account_id') != owner:
                app.remove_account(account)
    print('Teams sign-in saved in encrypted local storage.', flush=True)


def read_client(graph):
    if settings()['teams_auth']:
        access_token = token()
        # Reuse the installed Graph transport and TLS configuration, while
        # never loading, refreshing or writing its shared credential cache.
        class TeamsClient(type(graph)):
            def __init__(self):
                pass

            def get_token(self):
                return access_token
        return TeamsClient()
    # Refresh through the shared client before checking scopes, so a stale
    # access token cannot hide a newly consented permission.
    credential = graph.get_token() if hasattr(graph, 'get_token') else getattr(graph, '_access_token', '')
    if hasattr(graph, 'get_token') and not credential:
        raise ValueError('Sign in to Microsoft in Connections before marking a chat read.')
    info = claims(credential)
    if info and 'Chat.ReadWrite' not in info.get('scp', '').split():
        raise ValueError(SETUP)
    return graph


if __name__ == '__main__':
    try:
        sign_in()
    except Exception as error:
        print(str(error), flush=True)
        raise SystemExit(1)
