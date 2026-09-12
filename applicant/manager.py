"""Local launch/stop controls. Private configuration and logs stay outside the repo."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from applicant.worker import STATE

ROOT=Path(__file__).resolve().parents[1]


def alive():
    try:
        pid=int((STATE/'worker.pid').read_text())
        name=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
        return pid if 'applicant.worker' in name and str(ROOT/'.venv/bin/python') in name else None
    except (OSError,ValueError): return None


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['start','stop','status','connect']);p.add_argument('file',nargs='?');args=p.parse_args()
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.action=='connect':
        src=Path(args.file or Path.home()/'Downloads/radar-worker.json')
        data=json.loads(src.read_text())
        if data.get('url')!='https://internship-alerts-1412.onrender.com' or len(data.get('token',''))<40:
            raise SystemExit('Not a valid Radar connection file.')
        dest=STATE/'connection.json';dest.write_text(json.dumps(data));dest.chmod(0o600)
        print('Connected. Choose Start / Resume on the website, then launch the Mac worker.');return
    pid=alive()
    if args.action=='stop':
        if pid: os.kill(pid,signal.SIGTERM);print('Stop requested. The browser will close; any attempted submission stays recorded.')
        else: print('The local worker is not running.')
        return
    if args.action=='status':print('Running' if pid else 'Stopped');return
    if pid: print('Already running.');return
    if not (STATE/'connection.json').exists():
        print('Download the Mac connection from Radar > Auto apply first, then double-click Connect Radar Worker.command.');return
    model='qwen2.5:3b' if Path('/opt/homebrew/bin/ollama').exists() else ''
    log=STATE/'worker.log'
    with log.open('a') as output:
        proc=subprocess.Popen(['/usr/bin/nice','-n','10',str(ROOT/'.venv/bin/python'),'-m','applicant.worker','--model',model],
            cwd=ROOT,stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    log.chmod(0o600)
    print('Worker launched in the background. Use Radar > Auto apply to pause or stop, or double-click Stop Radar Auto Apply.command.')


if __name__=='__main__':main()
