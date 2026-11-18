"""Explicit opt-in Windows demo tunnel. All server listeners stay on loopback."""
import os
import socket
import threading
import select
import time
from ssh_client import connect

def pump(left,right):
    try:
        while True:
            ready,_,_=select.select([left,right],[],[],30)
            for src in ready:
                data=src.recv(65536)
                if not data:return
                (right if src is left else left).sendall(data)
    finally:left.close();right.close()

def reverse(channel,origin,server):
    try:local=socket.create_connection(('127.0.0.1',8112),timeout=10)
    except OSError:channel.close();return
    pump(channel,local)

def local_forward(client,port,destination):
    server=socket.socket();server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);server.bind(('127.0.0.1',port));server.listen(20);server.settimeout(2)
    transport=client.get_transport()
    try:
        while transport.is_active():
            try:incoming,address=server.accept()
            except socket.timeout:continue
            try:channel=transport.open_channel('direct-tcpip',('127.0.0.1',destination),address,timeout=10)
            except Exception:incoming.close();continue
            threading.Thread(target=pump,args=(incoming,channel),daemon=True).start()
    finally:server.close()

def main():
    while True:
        try:
            with connect() as client:
                transport=client.get_transport();transport.set_keepalive(20)
                transport.request_port_forward('127.0.0.1',18112,handler=lambda *args:threading.Thread(target=reverse,args=args,daemon=True).start())
                for local,remote in [(3210,3210),(3090,3090)]:threading.Thread(target=local_forward,args=(client,local,remote),daemon=True).start()
                print('DEMO_TUNNEL_CONNECTED: web 3210, Argo 3090; LLM forwarded privately',flush=True)
                while transport.is_active():time.sleep(2)
        except Exception as error:print('DEMO_TUNNEL_RECONNECT: '+type(error).__name__,flush=True)
        time.sleep(5)

if __name__=='__main__':main()
