"""Run the test suite in isolated schemas of the lab database, then close the tunnel."""
import json
import os
import socket
import subprocess
import sys
import time
from lab import ROOT,STATE,KUBECTL,CONFIG,CLUSTER,NAMESPACE,private_values

def main():
    args=[KUBECTL,'--kubeconfig',CONFIG,'--context','k3d-'+CLUSTER,'-n',NAMESPACE,'port-forward','service/postgres','55437:5432','--address','127.0.0.1']
    process=subprocess.Popen(args,stdout=(STATE/'test-pg-forward.log').open('wb'),stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        for _ in range(30):
            if process.poll() is not None:raise RuntimeError('Private test tunnel could not start')
            try:
                with socket.create_connection(('127.0.0.1',55437),timeout=1):break
            except OSError:time.sleep(1)
        else:raise RuntimeError('Private test tunnel timed out')
        url=private_values()['DATABASE_URL'].replace('@postgres:5432/','@127.0.0.1:55437/')
        result=subprocess.run([sys.executable,'-m','pytest','-q'],cwd=ROOT,env={**os.environ,'TEST_DATABASE_URL':url})
        (STATE/'postgres-tests.json').write_text(json.dumps({'passed':result.returncode==0,'database':'lab PostgreSQL; per-test temporary schemas','exit_code':result.returncode},indent=2)+'\n')
        raise SystemExit(result.returncode)
    finally:process.terminate();process.wait(timeout=10)

if __name__=='__main__':main()
