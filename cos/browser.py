"""Browser work uses a separate Edge profile and the same approval broker as other tools."""
import os
import threading
from pathlib import Path
from urllib.parse import urlparse
from .tools import workspace,file_record
from .config import settings
from .research import validate_url

LOCK=threading.Lock()
def browser_action(store,job,action,args):
    from playwright.sync_api import sync_playwright
    url=args['url']
    validate_url(url)
    read_only=action=='browser_read' or bool(args.get('_preparation_review'))
    profile=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'AMD-ChiefOfStaff/browser'
    with LOCK,sync_playwright() as p:
        browser=p.chromium.launch_persistent_context(str(profile),channel='msedge',headless=action=='browser_read',accept_downloads=False)
        try:
            page=browser.pages[0] if browser.pages else browser.new_page()
            # Only configured hosts and AMD/Microsoft SSO can be contacted. Third-party page content cannot open arbitrary destinations.
            allowed=set(settings()['research_hosts'])
            def guard(route):
                if read_only and route.request.method not in {'GET','HEAD','OPTIONS'}:
                    route.abort()
                    return
                target=urlparse(route.request.url)
                host=target.hostname or ''
                sso = host in {'login.microsoftonline.com','amdsso.okta.com','ok14static.oktacdn.com'}
                if route.request.is_navigation_request() and route.request.frame == page.main_frame and not (action=='browser_action' and sso and target.scheme=='https'):
                    try:
                        validate_url(route.request.url)
                    except ValueError:
                        route.abort()
                        return
                if target.scheme in {'data','blob'} or host in allowed or host.endswith('.amd.com') or host in {'login.microsoftonline.com','amdsso.okta.com','ok14static.oktacdn.com'}:
                    route.continue_()
                else:
                    route.abort()
            browser.route('**/*',guard)
            if read_only:
                # WebSocket messages can mutate business state without an HTTP
                # method. Automatically reviewed navigation cannot use them.
                browser.route_web_socket('**/*',lambda socket: socket.close())
            response=page.goto(url,wait_until='domcontentloaded',timeout=60000)
            if response is not None and response.status>=400:
                raise ValueError('Research page returned HTTP '+str(response.status)+'.')
            if action=='browser_action':
                for step in args.get('steps',[]):
                    locator=page.locator(step['selector'])
                    op=step['operation']
                    if op=='click':
                        locator.click(timeout=15000)
                    elif op=='fill':
                        locator.fill(step['value'])
                    elif op=='press':
                        locator.press(step['value'])
                    else:
                        raise ValueError('Supported operations: click, fill, press')
            text=page.locator('body').inner_text(timeout=15000)
            from .db import uid
            output=workspace(job['id'])/('browser-evidence-'+uid()+'.png')
            page.screenshot(path=str(output),full_page=True)
            return {'url':page.url,'text':text[:60000],'screenshot':file_record(store,job['id'],output),'path':str(output)}
        finally:
            browser.close()
