"""Túnel temporal para que Meta descargue un video directo desde este PC (vía file_url).
Por defecto usa localhost.run (SSH de Windows, sin cuenta): el túnel rápido de Cloudflare no sirve porque su
robots.txt gestionado bloquea al descargador de Meta (meta-externalfetcher).

Sirve UN solo archivo en 127.0.0.1 bajo una ruta aleatoria y lo expone con un túnel rápido de
Cloudflare (cloudflared, sin cuenta). Nada queda guardado fuera del PC; al cerrar el túnel el enlace muere.

    with TunelVideo(Path('video.mp4')) as url:
        ...  # pasar url a Meta en la cabecera file_url
"""
import re, secrets, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIN = Path(__file__).resolve().parent / 'bin' / 'cloudflared.exe'
SSH = r'C:\Windows\System32\OpenSSH\ssh.exe' if Path(r'C:\Windows\System32\OpenSSH\ssh.exe').exists() else 'ssh'


class TunelVideo:
    def __init__(self, video: Path, espera=60, proveedor='localhostrun'):
        self.video, self.espera, self.proveedor = Path(video), espera, proveedor
        self.ruta = f'/{secrets.token_urlsafe(16)}/{self.video.name}'
        self.descargas = 0
        self.registro = []  # (hora, método, ruta, user-agent, rango)

    def _servidor(self):
        tunel, video, ruta = self, self.video, self.ruta

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _anotar(self):
                tunel.registro.append((time.strftime('%H:%M:%S'), self.command, self.path[:40], self.headers.get('User-Agent', '')[:60], self.headers.get('Range', '')))

            def _cabeceras(self, codigo, largo, extra=None):
                self.send_response(codigo); self.send_header('Content-Type', 'video/mp4'); self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', str(largo))
                for k, v in (extra or {}).items(): self.send_header(k, v)
                self.end_headers()

            def _rango(self, n):
                m = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range', ''))
                if not m: return 0, n - 1, False
                a = int(m.group(1)) if m.group(1) else n - int(m.group(2)); b = int(m.group(2)) if m.group(1) and m.group(2) else n - 1
                return max(0, a), min(n - 1, b), True

            def _robots(self, cuerpo=True):  # el descargador de Meta respeta robots.txt: permitir solo nuestra ruta
                txt = f'User-agent: *\nAllow: {ruta}\n'.encode()
                self.send_response(200); self.send_header('Content-Type', 'text/plain'); self.send_header('Content-Length', str(len(txt))); self.end_headers()
                if cuerpo: self.wfile.write(txt)

            def do_HEAD(self):
                self._anotar()
                if self.path == '/robots.txt': return self._robots(False)
                if self.path != ruta: self.send_error(404); return
                self._cabeceras(200, video.stat().st_size)

            def do_GET(self):
                self._anotar()
                if self.path == '/robots.txt': return self._robots()
                if self.path != ruta: self.send_error(404); return
                n = video.stat().st_size; a, b, parcial = self._rango(n)
                self._cabeceras(206 if parcial else 200, b - a + 1, {'Content-Range': f'bytes {a}-{b}/{n}'} if parcial else None)
                tunel.descargas += 1
                with open(video, 'rb') as f:
                    f.seek(a); falta = b - a + 1
                    while falta > 0:
                        bloque = f.read(min(1 << 20, falta))
                        if not bloque: break
                        try: self.wfile.write(bloque)
                        except (BrokenPipeError, ConnectionResetError): return
                        falta -= len(bloque)

        return ThreadingHTTPServer(('127.0.0.1', 0), H)

    def _comando(self, puerto):
        if self.proveedor == 'cloudflare':  # OJO: su robots.txt gestionado bloquea a meta-externalfetcher
            return [str(BIN), 'tunnel', '--no-autoupdate', '--url', f'http://127.0.0.1:{puerto}'], r'https://[a-z0-9-]+\.trycloudflare\.com'
        known = Path(__file__).resolve().parent / 'bin' / 'known_hosts'
        if self.proveedor == 'serveo':
            return [SSH, '-T', '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={known}', '-o', 'ServerAliveInterval=20',
                    '-o', 'ConnectTimeout=15', '-o', 'ExitOnForwardFailure=yes', '-R', f'80:127.0.0.1:{puerto}', 'serveo.net'], r'https://[a-z0-9.-]+\.serveousercontent\.com'
        return [SSH, '-T', '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={known}', '-o', 'ServerAliveInterval=20',
                '-o', 'ExitOnForwardFailure=yes', '-R', f'80:127.0.0.1:{puerto}', 'nokey@localhost.run'], r'https://[a-z0-9.-]+\.lhr\.life'

    def __enter__(self):
        self.srv = self._servidor(); puerto = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        cmd, patron = self._comando(puerto)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                     errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        base, t0, salida = None, time.time(), []
        lector = threading.Thread(target=lambda: [salida.append(l) for l in self.proc.stdout], daemon=True); lector.start()
        while time.time() - t0 < self.espera and base is None:
            m = re.search(patron, ''.join(salida))
            if m: base = m.group(0); break
            if self.proc.poll() is not None: break
            time.sleep(0.5)
        if not base: self.__exit__(None, None, None); raise RuntimeError('el túnel no entregó un enlace público: ' + ''.join(salida)[-300:])
        self.url = base + self.ruta
        while time.time() - t0 < self.espera:  # esperar a que el DNS del túnel responda
            try:
                with urllib.request.urlopen(urllib.request.Request(self.url, method='HEAD'), timeout=10) as r:
                    if r.status == 200: return self.url
            except Exception: time.sleep(2)
        self.__exit__(None, None, None); raise RuntimeError('el túnel no respondió a tiempo')

    def __exit__(self, *exc):
        try: self.proc.terminate(); self.proc.wait(timeout=10)
        except Exception: pass
        try: self.srv.shutdown(); self.srv.server_close()
        except Exception: pass
