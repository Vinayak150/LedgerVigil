#!/usr/bin/env python3
"""Run the SANDBOX frontend and existing API on loopback; stop both on Ctrl+C."""
from pathlib import Path
import os, shutil, signal, socket, subprocess, sys, time
web=Path(__file__).resolve().parents[1]
app=web.parent
if not (app/'.venv/bin/python').exists() or not (app/'tools/serve_api.py').exists():
    raise SystemExit('Run this launcher from the delivered app/web checkout after backend setup.')
if not (web/'node_modules').exists():
    raise SystemExit('Install pinned frontend dependencies first: npm ci')
npm=shutil.which('npm')
if not npm: raise SystemExit('npm is required.')
for port in (3000,8000):
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1',port))==0: raise SystemExit(f'Port {port} is already in use. Stop that server before launching this stack.')
env=os.environ.copy();env['LV_ALLOWED_ORIGINS']='http://127.0.0.1:3000'
processes=[]
def stop(*_):
    for process in processes:
        if process.poll() is None:
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
    for process in processes:
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL)
signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
try:
    processes.append(subprocess.Popen([str(app/'.venv/bin/python'),'-m','tools.serve_api','--local-http'],cwd=app,env=env,start_new_session=True))
    processes.append(subprocess.Popen([npm,'run','dev'],cwd=web,env=env,start_new_session=True))
    print('Local SANDBOX: http://127.0.0.1:3000 — use 127.0.0.1 consistently. Ctrl+C stops both servers.',flush=True)
    while all(p.poll() is None for p in processes):time.sleep(.5)
    raise SystemExit('A server stopped. Check the output above; stopping its companion.')
except KeyboardInterrupt:pass
finally:stop()
