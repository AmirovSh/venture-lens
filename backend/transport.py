"""OpenAI-compatible transports. Windows mTLS uses the OS key store, never exports keys."""
import json
import ssl
import subprocess
import httpx
from .settings import BASE_URL, API_KEY, TRUST_ENV, setting
from email.utils import parsedate_to_datetime
from datetime import datetime,timezone

class TransportError(RuntimeError):
    def __init__(self,message,status=None,retry_after=0):
        super().__init__(message);self.status=status;self.retry_after=retry_after

def retry_delay(value):
    try:return max(0,min(3600,float(value)))
    except (ValueError,TypeError):
        try:return max(0,min(3600,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()))
        except (ValueError,TypeError,OverflowError):return 0

def completion(payload,timeout,route=None):
    route=route or {'url':BASE_URL,'key':API_KEY,'transport':setting('LLM_TRANSPORT'),'client_cert':setting('LLM_CLIENT_CERT')}
    if route.get('transport')=='schannel':
        quote=lambda s:'"'+s.replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r')+'"'
        config='url = '+quote(route['url']+'/chat/completions')+'\nheader = '+quote('Authorization: Bearer '+route['key'])+'\nheader = "Content-Type: application/json"\ndata = '+quote(json.dumps(payload,ensure_ascii=False))+'\n'
        cert_args=['--cert',route['client_cert']] if route.get('client_cert') else ['--ssl-auto-client-cert']
        args=['curl.exe','-q','--config','-',*cert_args,'--noproxy','*','--silent','--show-error','--max-time',str(timeout),'--write-out','\n%{http_code}\n%header{retry-after}']
        if setting('LLM_REVOCATION_BEST_EFFORT','false')=='true':args.append('--ssl-revoke-best-effort')
        result=subprocess.run(args,input=config.encode('utf-8'),capture_output=True,timeout=timeout+5)
        if result.returncode:raise TransportError(f'LLM transport: curl exit {result.returncode}')
        body,status,delay=result.stdout.rsplit(b'\n',2)
        if status!=b'200':raise TransportError(f'LLM HTTP {status.decode(errors="replace")}',int(status),retry_delay(delay.decode()))
        return json.loads(body)
    context=ssl.create_default_context(cafile=None if route.get('isolated_tls') else setting('LLM_CA_FILE') or None)
    if not route.get('isolated_tls') and setting('LLM_CLIENT_CERT_FILE'):context.load_cert_chain(setting('LLM_CLIENT_CERT_FILE'),setting('LLM_CLIENT_KEY_FILE') or None)
    with httpx.Client(timeout=timeout,trust_env=False if route.get('isolated_tls') else TRUST_ENV,verify=context) as c:
        response=c.post(route['url']+'/chat/completions',headers={'Authorization':'Bearer '+route['key']},json=payload)
        if not response.is_success:raise TransportError(f'LLM HTTP {response.status_code}',response.status_code,retry_delay(response.headers.get('retry-after')))
        return response.json()
