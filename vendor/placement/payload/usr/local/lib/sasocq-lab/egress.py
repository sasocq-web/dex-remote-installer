#!/usr/bin/python3
"""Public web egress through a Unix socket; no routes into host/LAN/VM."""
import ipaddress, os, pathlib, select, socket, socketserver, sys, threading, time
from urllib.parse import urlsplit
MAX_CONNECTIONS=32
slots=threading.BoundedSemaphore(MAX_CONNECTIONS)

def relay(a,b,initial=b''):
    if initial: b.sendall(initial)
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        ready,_,_=select.select([a,b],[],[],1)
        for src in ready:
            buf=src.recv(65536)
            if not buf: return
            (b if src is a else a).sendall(buf)
            deadline=time.monotonic()+120

def public_socket(host,port):
    if port not in (80,443) or not host or len(host)>253 or any(c.isspace() for c in host): raise ValueError('Destino recusado')
    addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses): raise ValueError('Endereço privado/local recusado')
    error=None
    for family,kind,proto,_,addr in addresses:
        s=socket.socket(family,kind,proto); s.settimeout(10)
        try: s.connect(addr); return s
        except OSError as e: s.close(); error=e
    raise error or ValueError('Destino indisponível')

class HostHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if not slots.acquire(False): return
        try:
            self.request.settimeout(15)
            data=b''
            while b'\r\n\r\n' not in data:
                block=self.request.recv(4096)
                if not block: return
                data+=block
                if len(data)>65536: raise ValueError('Cabeçalho excedeu o limite')
            header,tail=data.split(b'\r\n\r\n',1)
            lines=header.decode('iso-8859-1').split('\r\n')
            method,target,version=lines[0].split(' ',2)
            if method=='CONNECT':
                url=urlsplit('//'+target); host=url.hostname; port=url.port
                if url.username or url.password or url.path: raise ValueError('Destino inválido')
                upstream=public_socket(host,port)
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                first=tail
            else:
                url=urlsplit(target)
                if url.scheme!='http' or url.username or url.password or method not in ('GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'): raise ValueError('Pedido inválido')
                upstream=public_socket(url.hostname,url.port or 80)
                path=url.path or '/'
                if url.query: path+='?'+url.query
                clean=[l for l in lines[1:] if not l.lower().startswith(('proxy-','connection:'))]
                first=(method+' '+path+' HTTP/1.1\r\n'+'\r\n'.join(clean)+'\r\nConnection: close\r\n\r\n').encode('iso-8859-1')+tail
            with upstream: relay(self.request,upstream,first)
        except Exception:
            try: self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError: pass
        finally: slots.release()

class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if not slots.acquire(False): return
        try:
            with socket.socket(socket.AF_UNIX) as upstream:
                upstream.settimeout(15); upstream.connect('/control/egress.sock')
                self.request.settimeout(15); relay(self.request,upstream)
        except OSError: pass
        finally: slots.release()

class UnixServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer): daemon_threads=True
class TCPServer(socketserver.ThreadingMixIn,socketserver.TCPServer):
    daemon_threads=True; allow_reuse_address=True

def start_host(path):
    pathlib.Path(path).unlink(missing_ok=True)
    server=UnixServer(path,HostHandler); os.chmod(path,0o600)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server

if __name__=='__main__':
    if sys.argv[1:]!=['bridge']: sys.exit('Use bridge dentro do laboratório')
    with TCPServer(('127.0.0.1',3128),BridgeHandler) as server: server.serve_forever()
