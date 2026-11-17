"""OpenAI-compatible transports. Windows mTLS uses the OS key store, never exports keys."""
import json
import ssl
import subprocess
import httpx
from .settings import BASE_URL, API_KEY, TRUST_ENV, setting

class TransportError(RuntimeError): pass

def completion(payload,timeout):
    if setting('LLM_TRANSPORT')=='schannel':
        quote=lambda s:'"'+s.replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r')+'"'
        config='url = '+quote(BASE_URL+'/chat/completions')+'\nheader = '+quote('Authorization: Bearer '+API_KEY)+'\nheader = "Content-Type: application/json"\ndata = '+quote(json.dumps(payload,ensure_ascii=False))+'\n'
        args=['curl.exe','-q','--config','-','--ssl-auto-client-cert','--noproxy','*','--silent','--show-error','--max-time',str(timeout),'--write-out','\n%{http_code}']
        if setting('LLM_REVOCATION_BEST_EFFORT','false')=='true':args.append('--ssl-revoke-best-effort')
        result=subprocess.run(args,input=config.encode('utf-8'),capture_output=True,timeout=timeout+5)
        if result.returncode:raise TransportError(f'LLM transport: curl exit {result.returncode}')
        body,_,status=result.stdout.rpartition(b'\n')
        if status!=b'200':raise TransportError(f'LLM HTTP {status.decode(errors="replace")}')
        return json.loads(body)
    with httpx.Client(timeout=timeout,trust_env=TRUST_ENV,verify=ssl.create_default_context()) as c:
        response=c.post(BASE_URL+'/chat/completions',headers={'Authorization':'Bearer '+API_KEY},json=payload)
        if not response.is_success:raise TransportError(f'LLM HTTP {response.status_code}')
        return response.json()
