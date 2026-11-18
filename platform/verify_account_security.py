"""Live HTTPS check using only our two existing QA accounts; no paid research.

Never prints passwords, reset URLs, tokens, contacts or cookies.
The temporary QA administrator is returned to its original role in finally.
"""
import http.client
import http.cookies
import json
import secrets
import ssl
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
import socks
from ssh_client import ROOT
from configure_public_demo import private_permissions
from public_settings import public_origin


def main():
    path=ROOT/'.platform/registration-check-accounts.json'
    qa=json.loads(path.read_text())
    access=json.loads((ROOT/'.platform/public-demo-credentials.json').read_text())
    ssh=json.loads((ROOT/'.platform/server.json').read_text())
    subject,helper=qa['accounts'][:2]
    origin=public_origin();host=urlsplit(origin).hostname
    context=ssl.create_default_context()

    def request(path,method='GET',body=None,token=None):
        sock=socks.socksocket()
        if ssh.get('socks'):sock.set_proxy(socks.SOCKS5,ssh['socks']['host'],ssh['socks']['port'])
        sock.settimeout(20);sock.connect((host,443));sock=context.wrap_socket(sock,server_hostname=host)
        client=http.client.HTTPConnection(host,timeout=20);client.sock=sock
        headers={}
        if token:headers['Cookie']='__Host-venturelens_session='+token
        if method!='GET':headers['Origin']=origin
        if isinstance(body,dict):body=json.dumps(body).encode();headers['Content-Type']='application/json'
        try:
            client.request(method,path,body=body,headers=headers);r=client.getresponse()
            return r.status,dict(r.headers),r.read()
        finally:client.close()

    def login(account,password=None,expected=303):
        status,headers,_=request('/auth/login','POST',urlencode({'username':account['username'],'password':password or account['password']}).encode())
        assert status==expected,'QA login check failed: HTTP '+str(status)
        if status!=303:return None
        cookie=http.cookies.SimpleCookie();cookie.load(headers['Set-Cookie'])
        return cookie['__Host-venturelens_session'].value

    def get(path,token):
        status,_,body=request(path,token=token);assert status==200,'Read failed: HTTP '+str(status)
        return json.loads(body)

    def post(path,body,token,expected=200):
        status,_,result=request(path,'POST',body,token);assert status==expected,'Mutation failed: HTTP '+str(status)
        return json.loads(result) if expected==200 else None

    def save_password(value):
        subject['password']=value;path.write_text(json.dumps(qa,ensure_ascii=False,indent=2)+'\n');private_permissions(path)

    owner=login(access['owner']);subject_token=login(subject)
    workspace=get('/auth/session',subject_token)['workspace']
    users=get('/auth/admin/users',owner)
    original_role=next(user['role'] for user in users if user['username']==helper['username'])
    assert get('/auth/session',owner)['role']=='owner'
    promoted=False
    proof={'url':origin,'checked_at':datetime.now(timezone.utc).isoformat(),'owner_exists':True}
    try:
        post('/auth/admin/users/'+helper['username']+'/role',{'role':'admin','current_password':access['owner']['password']},owner);promoted=True
        admin=login(helper)
        assert get('/auth/session',admin)['role']=='admin'
        assert get('/api/runs',admin)==[]
        assert request('/auth/admin/users',token=admin)[0]==403
        old_password=subject['password'];changed=secrets.token_urlsafe(24)
        post('/auth/password',{'current_password':old_password,'new_password':changed,'password_confirm':changed},subject_token)
        save_password(changed)
        assert request('/auth/session',token=subject_token)[0]==401
        login(subject,password=old_password,expected=401)
        changed_token=login(subject)
        body=urlencode({'username':subject['username'],'contact':'QA verification only','message':'Automated functional check; no external delivery required.'}).encode()
        assert request('/auth/recover','POST',body)[0]==202
        assert request('/auth/admin/recovery-requests',token=changed_token)[0]==403
        row=next(row for row in get('/auth/admin/recovery-requests',admin) if row['username']==subject['username'] and row['status']=='pending')
        result=post('/auth/admin/recovery-requests/'+row['id']+'/handle',{'action':'approve','identity_verified':True,'current_password':helper['password'],'expected_updated':str(row['updated'])},admin)
        assert result['expires_in']==900 and result['reset_url'].startswith(origin+'/auth/reset#')
        reset_token=result['reset_url'].split('#',1)[1]
        assert reset_token.encode() not in request('/auth/admin/recovery-requests',token=admin)[2]
        final_password=secrets.token_urlsafe(24)
        body=urlencode({'token':reset_token,'password':final_password,'password_confirm':final_password}).encode()
        assert request('/auth/reset','POST',body)[0]==303
        save_password(final_password)
        assert request('/auth/reset','POST',body)[0]==400
        assert request('/auth/session',token=changed_token)[0]==401
        final=login(subject)
        assert get('/auth/session',final)['workspace']==workspace
        assert get('/api/runs',final)==[]
        request('/auth/logout','POST',b'',final)
        proof.update({'password_change':True,'old_password_rejected':True,'old_sessions_revoked':True,'owner_assigns_admin':True,
            'administrator_handles_request':True,'member_cannot_read_queue':True,'administrator_cannot_assign_roles':True,
            'reset_link_single_use':True,'reset_link_hidden_from_queue':True,'registered_user_relogin':True,'workspace_preserved':True,
            'no_paid_research_started':True})
    finally:
        if promoted:
            post('/auth/admin/users/'+helper['username']+'/role',{'role':original_role,'current_password':access['owner']['password']},owner)
            proof['temporary_admin_role_restored']=True
        request('/auth/logout','POST',b'',owner)
    (ROOT/'.platform/account-security-report.json').write_text(json.dumps(proof,indent=2)+'\n')
    print(json.dumps(proof,indent=2))


if __name__=='__main__':main()
