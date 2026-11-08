from pathlib import Path
import subprocess,socket,time,json,urllib.request,os
r=Path(__file__).resolve().parents[1]
# Reserve a loopback socket and hand it to uvicorn, avoiding another service's port.
s=socket.socket();s.bind(('127.0.0.1',0));s.listen();port=s.getsockname()[1]
with (r/'evidence/http-smoke-server.txt').open('w') as log:
 p=subprocess.Popen([str(r/'.venv/bin/python'),'-m','uvicorn','ledgervigil.app:app','--fd',str(s.fileno())],cwd=r,pass_fds=(s.fileno(),),stdout=log,stderr=subprocess.STDOUT)
 s.close()
 try:
  for _ in range(50):
   if p.poll() is not None: raise RuntimeError('API exited early')
   try:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as response:
     result={'status_code':response.status,'body':json.load(response)}
    break
   except OSError: time.sleep(.1)
  else: raise RuntimeError('API did not become available')
  assert result=={'status_code':200,'body':{'status':'ok','milestone':'development-foundation'}}
 finally:
  p.terminate();p.wait(timeout=10)
result['temporary_server_stopped']=True
(r/'evidence/http-smoke.json').write_text(json.dumps(result,indent=2)+'\n')
print('Real loopback HTTP smoke passed; temporary API stopped.')
