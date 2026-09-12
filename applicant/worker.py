"""Isolated headless browser worker. No access to the owner's normal Chrome profile."""
import argparse
import asyncio
import contextlib
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
from urllib.parse import urlsplit
import httpx
from playwright.async_api import async_playwright
from applicant.forms import supported, fill_form, normalize

STATE = Path.home() / 'Library/Application Support/Internship Radar Worker'
STOP = asyncio.Event()


class Connection:
    def __init__(self, config):
        self.url=config['url'].rstrip('/')
        if self.url != 'https://internship-alerts-1412.onrender.com':
            raise ValueError('The connection file must point to your Internship Radar website.')
        self.client=httpx.AsyncClient(timeout=20,follow_redirects=False,headers={'Authorization':'Bearer '+config['token']})
    async def call(self,action,task=None,**data):
        if task: data.update(task_id=task['id'],claim_token=task['claim_token'])
        r=await self.client.post(self.url+'/integrations/auto-apply',json={'action':action,**data})
        r.raise_for_status(); return r.json()


async def public_request(route):
    url=urlsplit(route.request.url)
    if url.scheme not in ('http','https') or url.username or url.password:
        await route.abort(); return
    try:
        addresses=await asyncio.to_thread(socket.getaddrinfo,url.hostname,url.port or (443 if url.scheme=='https' else 80))
        safe=bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (OSError,ValueError): safe=False
    if not safe:
        await route.abort(); return
    # The browser may load public assets, but sends application forms only to supported ATS hosts.
    if route.request.method not in ('GET','HEAD','OPTIONS') and not supported(route.request.url):
        await route.abort(); return
    await route.continue_()


async def prepare(page, task, local_ai, model):
    if not supported(task['url']):
        return None,['This employer form is not supported yet. Apply on the employer site.']
    await page.goto(task['url'],wait_until='domcontentloaded',timeout=45000)
    await page.wait_for_timeout(1500)
    body=await page.locator('body').inner_text()
    # Require identity in the rendered job, not just the URL from the feed.
    title_words=[w for w in normalize(task['title']).split() if w not in ('2027','summer','internship','intern')]
    if not title_words or any(w not in normalize(body).split() for w in title_words):
        return None,['The employer page could not be confirmed as the selected role.']
    from poller.application_links import _company_key
    company_words = _company_key(task['company']).split()
    identity_text = normalize(body+' '+page.url)
    if not company_words or any(w not in identity_text.split() for w in company_words):
        return None,['Could not confirm the employer identity on the application page.']
    if re.search(r'verify you are human|complete the captcha|access denied|sign in to your account|create an account to apply',body,re.I):
        return None,['Employer login or human verification required.']
    from shared.eligibility import restriction_reasons
    if restriction_reasons(task['title'],body):
        return None,['Employer page lists an eligibility restriction. Review it before applying.']
    frames=[f for f in page.frames if supported(f.url)]
    for frame in frames:
        # Only a real submit control permits this adapter to submit. Multi-step login/application
        # flows are deliberately handed back until that employer adapter is supported.
        submit=frame.get_by_role('button',name=re.compile(r'^(submit application|submit your application|send application|apply now)$',re.I))
        if await submit.count()!=1: continue
        missing,uploads=await fill_form(frame,task,local_ai,model)
        if not uploads: missing.append('Could not confirm the resume attachment control')
        if missing: return None,missing
        return submit,[]
    return None,['This employer uses a login or application flow that needs manual completion.']


async def watch(connection, task, browser, halted):
    while not halted.is_set():
        try:
            result=await connection.call('heartbeat',task)
            allowed=result.get('allowed',False)
        except Exception:
            allowed=False
        if STOP.is_set() or not allowed:
            halted.set()
            await browser.close()
            return
        await asyncio.sleep(2)


async def execute(connection, task, playwright, model):
    browser=await playwright.chromium.launch(headless=True)
    halted=asyncio.Event()
    monitor=asyncio.create_task(watch(connection,task,browser,halted))
    started=False
    confirmation_pattern = r'(?:your application (?:has been|was) (?:successfully )?(?:submitted|received)|thank you for (?:applying|your application)|application submitted successfully)'
    try:
        context=await browser.new_context(service_workers='block',accept_downloads=False)
        await context.route('**/*',public_request)
        page=await context.new_page()
        page.set_default_timeout(6000)
        async with httpx.AsyncClient() as local_ai:
            submit,questions=await prepare(page,task,local_ai,model)
        if halted.is_set() or STOP.is_set(): return
        if not submit:
            await connection.call('result',task,state='needs_input',questions=questions,detail='Needs your input before submission.')
            return
        for frame in page.frames:
            if supported(frame.url) and re.search(confirmation_pattern, await frame.locator('body').inner_text(), re.I):
                await connection.call('result',task,state='needs_input',detail='Existing confirmation text makes automatic submission ambiguous.',questions=[])
                return
        permit=await connection.call('permit',task)
        if not permit.get('allowed') or STOP.is_set() or halted.is_set(): return
        started=True
        await submit.click(timeout=8000)
        confirmation=None
        # Confirmation must appear after clicking, in an employer-controlled frame.
        for _ in range(20):
            if halted.is_set(): break
            for frame in page.frames:
                if not supported(frame.url): continue
                text=await frame.locator('body').inner_text()
                match=re.search(confirmation_pattern,text,re.I)
                if match: confirmation=match.group(0); break
            if confirmation: break
            await asyncio.sleep(1)
        await connection.call('result',task,state='submitted' if confirmation else 'uncertain',confirmation=confirmation or '')
    except Exception:
        # Never log applicant data, URLs containing secrets, or page contents.
        with contextlib.suppress(Exception):
            await connection.call('result',task,state='uncertain' if started else 'needs_input',
                detail='Browser could not finish this application. Open the employer page to continue.',questions=[])
    finally:
        halted.set(); monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError,Exception): await monitor
        await browser.close()


async def run(config,model):
    connection=Connection(config)
    model_server=None
    if model:
        try:
            async with httpx.AsyncClient(timeout=2) as probe:
                response=await probe.get('http://127.0.0.1:11434/api/tags')
                response.raise_for_status()
        except httpx.HTTPError:
            model_server=subprocess.Popen(['/opt/homebrew/bin/ollama','serve'],
                env=dict(os.environ,OLLAMA_NO_CLOUD='1',OLLAMA_NUM_PARALLEL='1',OLLAMA_MAX_LOADED_MODELS='1'),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,STOP.set)
    try:
        async with async_playwright() as p:
            while not STOP.is_set():
                try: result=await connection.call('claim')
                except Exception:
                    print('Connection unavailable. No application submitted.',flush=True)
                    try: await asyncio.wait_for(STOP.wait(),timeout=20)
                    except asyncio.TimeoutError: pass
                    continue
                if result['mode']=='stopped': break
                if result.get('task'):
                    print('Preparing one application in the background.',flush=True)
                    await execute(connection,result['task'],p,model)
                else:
                    try: await asyncio.wait_for(STOP.wait(),timeout=5)
                    except asyncio.TimeoutError: pass
    finally:
        if STOP.is_set():
            with contextlib.suppress(Exception): await connection.call('local_stop')
        await connection.client.aclose()
        if model_server:
            model_server.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired): model_server.wait(timeout=5)
        print('Worker stopped. Browser closed.',flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default=str(STATE/'connection.json'))
    parser.add_argument('--model',default='')
    args=parser.parse_args(argv)
    if not Path(args.config).exists():
        print("Worker is disconnected. Download and import your Mac connection first.")
        return 0
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (STATE/'worker.lock').open('w') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('The worker is already running.'); return
        pid=STATE/'worker.pid'; pid.write_text(str(os.getpid()))
        try: asyncio.run(run(json.loads(Path(args.config).read_text()),args.model))
        finally: pid.unlink(missing_ok=True)


if __name__=='__main__': main()
