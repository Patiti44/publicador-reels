"""Sirve un video por unos minutos en GitHub Pages (repo público Patiti44/videos-reels) para que Meta lo descargue.

Uso:
    with VideoEnPages(video, config) as url:
        ...  # pedirle a Meta que descargue `url` (cabecera file_url)
Al salir, el repo vuelve a quedar vacío (force-push de un commit sin el video).
Necesita la llave de despliegue del repo en el archivo indicado por la variable VIDEOS_DEPLOY_KEY_FILE
(en la nube viene del secreto VIDEOS_DEPLOY_KEY) o, si no está, usa las credenciales normales de git.
"""
import os, secrets, shutil, subprocess, tempfile, time, urllib.request
from pathlib import Path

REPO_SSH = 'git@github.com:Patiti44/videos-reels.git'
REPO_HTTPS = 'https://github.com/Patiti44/videos-reels.git'
URL_BASE = 'https://patiti44.github.io/videos-reels/'
PORTADA = '<!doctype html><meta charset="utf-8"><title>videos</title>'


def _git(cwd, *args, env=None):
    r = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, env=env)
    if r.returncode != 0: raise RuntimeError(f"git {args[0]} falló: {(r.stderr or r.stdout)[-300:]}")
    return r.stdout


class VideoEnPages:
    def __init__(self, video: Path, c=None, espera=420):
        self.video, self.c, self.espera = Path(video), c or {}, espera
        self.dir = None

    def _entorno(self):
        env = dict(os.environ)
        llave = os.environ.get('VIDEOS_DEPLOY_KEY_FILE')
        if llave and Path(llave).exists():
            env['GIT_SSH_COMMAND'] = f'ssh -i "{llave}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new'
            return env, REPO_SSH
        return env, REPO_HTTPS

    def _empujar(self, archivos, mensaje):
        """Reemplaza todo el contenido del repo por `archivos` (dict nombre -> Path o texto) en un commit sin historia."""
        env, repo = self._entorno()
        d = Path(tempfile.mkdtemp(prefix='pages_'))
        try:
            _git(d, 'init', '-q', '-b', 'main', env=env)
            _git(d, 'config', 'user.name', 'publicador-reels', env=env)
            _git(d, 'config', 'user.email', 'publicador-reels@users.noreply.github.com', env=env)
            for nombre, cont in archivos.items():
                if isinstance(cont, Path): shutil.copy2(cont, d / nombre)
                else: (d / nombre).write_text(cont, encoding='utf-8')
            _git(d, 'add', '-A', env=env)
            _git(d, 'commit', '-q', '-m', mensaje, env=env)
            _git(d, 'push', '-q', '--force', repo, 'main', env=env)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def __enter__(self):
        nombre = f'{secrets.token_hex(12)}.mp4'
        self._empujar({'.nojekyll': '', 'index.html': PORTADA, nombre: self.video}, 'video temporal')
        url, n, t0 = URL_BASE + nombre, self.video.stat().st_size, time.time()
        while time.time() - t0 < self.espera:  # esperar a que GitHub Pages lo publique
            try:
                req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'publicador-reels'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    if r.status == 200 and int(r.headers.get('Content-Length', 0)) == n: return url
            except Exception: pass
            time.sleep(8)
        self._limpiar()
        raise RuntimeError(f'GitHub Pages no publicó el video en {self.espera} s')

    def _limpiar(self):
        try: self._empujar({'.nojekyll': '', 'index.html': PORTADA}, 'vacío')
        except RuntimeError: pass

    def __exit__(self, *exc):
        self._limpiar()
        return False
