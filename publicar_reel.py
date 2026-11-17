"""Publicador automático de Reels en Instagram (API oficial de publicación de contenido).

Funciona con las dos vías de Meta:
  - "instagram": Instagram API with Instagram Login (graph.instagram.com, token de 60 días que se renueva solo).
  - "facebook": inicio de sesión unificado / con Facebook (graph.facebook.com, token de usuario del sistema que no caduca).
`verificar` detecta sola cuál corresponde a tu token y la guarda en config.json (campo "api").
El video se sube desde tu PC (subida reanudable), así que no hace falta alojarlo en internet.
Solo usa la biblioteca estándar de Python.

Comandos (desde la carpeta del proyecto):
  python publicar/publicar_reel.py verificar              comprueba token, cuenta y cupo diario
  python publicar/publicar_reel.py agregar VIDEO "FECHA" --texto "..."   (o --texto-archivo post.txt)
  python publicar/publicar_reel.py cola                   muestra la cola
  python publicar/publicar_reel.py ejecutar [--simular]   publica lo que ya toca (lo usa la tarea programada)
  python publicar/publicar_reel.py renovar                renueva el token (solo vía "instagram"; dura 60 días)

FECHA en hora local: "2026-11-13 19:00".
"""
import argparse, json, os, sys, time, datetime as dt
import urllib.request, urllib.parse, urllib.error
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = Path(os.environ.get('PUBLICAR_RAIZ', AQUI.parent))  # carpeta base de las rutas de video de la cola
DATOS = Path(os.environ.get('PUBLICAR_DATOS', AQUI))  # carpeta de config/cola/registro (para pruebas)
CONFIG = DATOS / 'config.json'
COLA = DATOS / 'cola.json'
REGISTRO = DATOS / 'registro.log'
FMT = '%Y-%m-%d %H:%M'


# ---------------- utilidades ----------------
def log(msg):
    linea = f"[{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try: print(linea, flush=True)
    except UnicodeEncodeError: print(linea.encode('ascii', 'replace').decode(), flush=True)  # consola de Windows sin UTF-8
    with open(REGISTRO, 'a', encoding='utf-8') as f: f.write(linea + '\n')


def leer(p, defecto):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else defecto


def guardar(p, data):
    tmp = p.with_suffix('.tmp'); tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'); tmp.replace(p)


def cargar_config():
    if not CONFIG.exists(): sys.exit('Falta publicar/config.json. Copia config.ejemplo.json a config.json y completa el token (ver GUIA.md).')
    c = leer(CONFIG, {})
    if not c.get('access_token') or 'PEGA' in c['access_token']: sys.exit('config.json no tiene un access_token válido (ver GUIA.md).')
    c.setdefault('api_version', 'v23.0'); c.setdefault('upload_host', 'https://rupload.facebook.com'); c.setdefault('api', 'auto')
    c.setdefault('host_instagram', 'https://graph.instagram.com'); c.setdefault('host_facebook', 'https://graph.facebook.com')
    if not c.get('graph_host'): c['graph_host'] = c['host_facebook'] if c['api'] == 'facebook' else c['host_instagram']
    return c


class ErrorAPI(Exception): pass


def http(metodo, url, params=None, headers=None, body=None, timeout=120):
    if params: url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=body, method=metodo, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        txt = e.read().decode('utf-8', 'replace')
        try: msg = json.loads(txt).get('error', {}).get('message', txt)
        except Exception: msg = txt
        raise ErrorAPI(f'HTTP {e.code}: {msg}')
    except urllib.error.URLError as e:
        raise ErrorAPI(f'sin conexión: {e.reason}')
    except (OSError, ValueError, __import__('http.client').client.HTTPException) as e:
        raise ErrorAPI(f'fallo de red o respuesta inválida: {e!r}')


class API:
    def __init__(self, c, simular=False):
        self.c, self.sim = c, simular
        self.g = f"{c['graph_host'].rstrip('/')}/{c['api_version']}"

    def _tok(self, p=None): return {**(p or {}), 'access_token': self.c['access_token']}

    def get(self, path, p=None): return http('GET', f'{self.g}/{path}', self._tok(p))

    def post(self, path, p=None):
        if self.sim: log(f'  [simulación] POST {path} {json.dumps({k: v for k, v in (p or {}).items() if k != "caption"}, ensure_ascii=False)}'); return {'id': 'SIMULADO'}
        # parámetros en el cuerpo (form), no en la URL: el texto del post puede ser largo
        return http('POST', f'{self.g}/{path}', body=urllib.parse.urlencode(self._tok(p)).encode('utf-8'),
                    headers={'Content-Type': 'application/x-www-form-urlencoded'})

    def subir(self, contenedor, video: Path, uri=None):
        n = video.stat().st_size
        # Meta devuelve la URL de subida exacta (con su versión) al crear el contenedor: usarla siempre que venga
        url = uri or f"{self.c['upload_host'].rstrip('/')}/ig-api-upload/{self.c['api_version']}/{contenedor}"
        if self.sim: log(f'  [simulación] subida de {n/1e6:.1f} MB a {url}'); return {'success': True}
        h = {'Authorization': f"OAuth {self.c['access_token']}", 'offset': '0', 'file_size': str(n)}
        return http('POST', url, headers=h, body=video.read_bytes(), timeout=900)


def user_id(api):
    if api.c.get('ig_user_id'): return api.c['ig_user_id']
    if api.c.get('api') == 'facebook': raise ErrorAPI('falta ig_user_id en config.json: corre "verificar" primero')
    me = api.get('me', {'fields': 'user_id,username'})
    return me.get('user_id') or me['id']


def detectar_cuenta(c):
    """Devuelve (via, host, cuenta). Prueba la vía configurada o, con api='auto', ambas."""
    errores = []
    vias = [c['api']] if c['api'] in ('instagram', 'facebook') else ['instagram', 'facebook']
    for via in vias:
        host = c['host_instagram'] if via == 'instagram' else c['host_facebook']
        api = API({**c, 'graph_host': host})
        try:
            if via == 'instagram':
                me = api.get('me', {'fields': 'user_id,username,account_type,media_count'})
                return via, host, {'id': me.get('user_id') or me.get('id'), 'username': me.get('username'), 'tipo': me.get('account_type', '?'), 'media': me.get('media_count', '?')}
            campos = {'fields': 'id,username,media_count'}
            if c.get('ig_user_id'):
                d = api.get(c['ig_user_id'], campos)
                return via, host, {'id': d['id'], 'username': d.get('username'), 'tipo': 'profesional', 'media': d.get('media_count', '?')}
            r = api.get('me/accounts', {'fields': 'name,instagram_business_account{id,username,media_count}'})
            igs = [x['instagram_business_account'] for x in r.get('data', []) if x.get('instagram_business_account')]
            if not igs:
                try:
                    r2 = api.get('me', {'fields': 'id,name,instagram_accounts{id,username,media_count}'})
                    igs = (r2.get('instagram_accounts') or {}).get('data', [])
                except ErrorAPI: pass
            if not igs: raise ErrorAPI('el token sirve en graph.facebook.com pero no trae ninguna cuenta de Instagram; agrega "ig_user_id" en config.json')
            if len(igs) > 1: print('Hay varias cuentas de Instagram; uso la primera:', ', '.join('@' + str(x.get('username')) for x in igs))
            d = igs[0]
            return via, host, {'id': d['id'], 'username': d.get('username'), 'tipo': 'profesional', 'media': d.get('media_count', '?')}
        except ErrorAPI as e: errores.append(f'{via}: {e}')
    raise ErrorAPI('no pude usar el token con ninguna vía -> ' + ' | '.join(errores))


# ---------------- comandos ----------------
def cmd_verificar(a):
    c = cargar_config()
    via, host, me = detectar_cuenta(c)
    uid = me['id']; c['api'], c['graph_host'] = via, host; api = API(c)
    print(f"Cuenta: @{me.get('username')} · tipo {me['tipo']} · {me['media']} publicaciones · id {uid} · vía {via} ({host})")
    try:
        lim = api.get(f'{uid}/content_publishing_limit', {'fields': 'quota_usage,config'})
        d = (lim.get('data') or [{}])[0]
        print(f"Cupo de publicación (24 h): usados {d.get('quota_usage', '?')} de {d.get('config', {}).get('quota_total', '?')}")
    except ErrorAPI as e: print('No se pudo leer el cupo:', e)
    ren = c.get('token_renovado')
    if via == 'facebook': print('Token de la vía unificada/Facebook: no necesita renovación.')
    elif ren:
        dias = (dt.date.today() - dt.date.fromisoformat(ren)).days
        print(f'Token renovado hace {dias} días (caduca a los 60; se renueva solo al pasar los 30).')
    guardado = leer(CONFIG, {})
    if guardado.get('ig_user_id') != uid or guardado.get('api') != via or guardado.get('graph_host') != host:
        guardado.update({'ig_user_id': uid, 'api': via, 'graph_host': host}); guardar(CONFIG, guardado); print('Guardé ig_user_id y la vía en config.json.')


def cmd_renovar(a, c=None):
    c = c or cargar_config()
    if c.get('api') == 'facebook': print('Este token (vía unificada/Facebook) no caduca: no hay nada que renovar.'); return
    r = http('GET', f"{c['graph_host'].rstrip('/')}/refresh_access_token", {'grant_type': 'ig_refresh_token', 'access_token': c['access_token']})
    c['access_token'] = r['access_token']; c['token_renovado'] = dt.date.today().isoformat(); guardar(CONFIG, c)
    log(f"Token renovado (válido unos {int(r.get('expires_in', 0)) // 86400} días).")


def cmd_agregar(a):
    video = Path(a.video).resolve()
    if not video.exists(): sys.exit(f'No existe el video: {video}')
    fecha = dt.datetime.strptime(a.fecha, FMT)
    texto = Path(a.texto_archivo).read_text(encoding='utf-8').strip() if a.texto_archivo else (a.texto or '')
    if len(texto) > 2200: sys.exit(f'El texto tiene {len(texto)} caracteres; Instagram admite hasta 2200.')
    if texto.count('#') > 30: sys.exit('Instagram admite hasta 30 hashtags.')
    cola = leer(COLA, [])
    nuevo = {'id': max([x['id'] for x in cola] + [0]) + 1, 'video': str(video.relative_to(RAIZ)) if video.is_relative_to(RAIZ) else str(video),
             'texto': texto, 'publicar_en': fecha.strftime(FMT), 'portada_ms': a.portada_ms, 'estado': 'pendiente'}
    cola.append(nuevo); guardar(COLA, cola)
    print(f"Agregado #{nuevo['id']}: {nuevo['video']} → {nuevo['publicar_en']}")


def cmd_cola(a):
    cola = leer(COLA, [])
    if not cola: print('La cola está vacía.'); return
    for x in sorted(cola, key=lambda x: x['publicar_en']):
        extra = x.get('permalink') or x.get('error') or ''
        print(f"#{x['id']:<3} {x['publicar_en']}  {x['estado']:<11} {Path(x['video']).name}  {extra}")


def version_instagram(video: Path) -> Path:
    """Versión liviana para Instagram (720x1280, ~5 MB por minuto). Meta solo completa descargas cortas,
    e Instagram recomprime igual. Queda en publicar/cache/ y se reutiliza."""
    if video.stem.endswith(('_ig', '_ig720')): return video  # ya viene optimizado (p. ej. en la nube)
    import shutil, subprocess
    if not shutil.which('ffmpeg') and os.environ.get('GITHUB_ACTIONS'):  # el runner de GitHub no trae ffmpeg
        log('  instalando ffmpeg en el runner…')
        subprocess.run('sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg', shell=True, capture_output=True)
    base = video.stem[:-3] if video.stem.endswith('_hd') else video.stem
    destino = AQUI / 'cache' / (base + '_ig720.mp4'); destino.parent.mkdir(exist_ok=True)
    if destino.exists() and destino.stat().st_mtime >= video.stat().st_mtime: return destino
    import subprocess
    cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(video), '-vf', 'scale=720:1280:flags=lanczos', '-c:v', 'libx264', '-preset', 'slow',
           '-crf', '24', '-maxrate', '900k', '-bufsize', '1800k', '-pix_fmt', 'yuv420p', '-r', '30', '-c:a', 'aac', '-b:a', '96k', '-ar', '48000',
           '-movflags', '+faststart', str(destino)]
    r = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode != 0: raise ErrorAPI(f'ffmpeg no pudo preparar el video: {r.stderr[-300:]}')
    log(f'  versión para Instagram: {destino.name} ({destino.stat().st_size / 1e6:.1f} MB)')
    return destino


def version_hd(video: Path) -> Path:
    """Versión 1080x1920 para subir por GitHub Pages. Meta rechaza (ProcessingFailedError) los archivos grandes:
    24 MB siempre falla y ~10 MB a veces (pruebas del 17-11-2026), así que se apunta a <= 8 MB."""
    if video.stem.endswith('_hd'): return video
    destino = AQUI / 'cache' / (video.stem + '_hd.mp4'); destino.parent.mkdir(exist_ok=True)
    if destino.exists() and destino.stat().st_mtime >= video.stat().st_mtime: return destino
    import subprocess
    for crf, maxrate in ((22, 1400), (24, 1150), (26, 950)):
        cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(video), '-vf', 'scale=1080:1920:flags=lanczos', '-c:v', 'libx264', '-preset', 'slow',
               '-profile:v', 'high', '-level:v', '4.0', '-crf', str(crf), '-maxrate', f'{maxrate}k', '-bufsize', f'{2 * maxrate}k',
               '-pix_fmt', 'yuv420p', '-r', '30', '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2', '-movflags', '+faststart', str(destino)]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode != 0: raise ErrorAPI(f'ffmpeg no pudo preparar el video: {r.stderr[-300:]}')
        if destino.stat().st_size <= 8.0e6: break
    log(f'  versión HD: {destino.name} ({destino.stat().st_size / 1e6:.1f} MB, crf {crf})')
    return destino


def subir_video(api, contenedor, uri, video: Path):
    """Hace que Meta descargue el video. Modo 'pages': lo sirve unos minutos en GitHub Pages;
    si Pages no llega a publicarlo, cae al túnel con la versión liviana de 720p."""
    url = uri or f"{api.c['upload_host'].rstrip('/')}/ig-api-upload/{api.c['api_version']}/{contenedor}"
    if api.c.get('subida') == 'pages':
        try:
            from pages import VideoEnPages
            log(f'  publicando {video.name} ({video.stat().st_size / 1e6:.1f} MB) en GitHub Pages…')
            with VideoEnPages(video, api.c) as enlace:
                log('  GitHub Pages listo; Meta descarga el video…')
                h = {'Authorization': f"OAuth {api.c['access_token']}", 'file_url': enlace}
                return http('POST', url, headers=h, body=b'', timeout=300)
        except RuntimeError as e:
            log(f'  GitHub Pages falló ({e}); se intenta por túnel en 720p')
        video = version_instagram(video)
    return subir_por_tunel(api, contenedor, uri, video)


def subir_por_tunel(api, contenedor, uri, video: Path):
    """Abre un túnel temporal hacia este PC y le pide a Meta que descargue el video (cabecera file_url)."""
    from tunel import TunelVideo
    url = uri or f"{api.c['upload_host'].rstrip('/')}/ig-api-upload/{api.c['api_version']}/{contenedor}"
    errores = []
    for prov in api.c.get('tuneles', ['serveo', 'localhostrun']):
        try:
            with TunelVideo(video, proveedor=prov) as enlace:
                log(f'  túnel {prov} abierto; Meta descarga el video…')
                h = {'Authorization': f"OAuth {api.c['access_token']}", 'file_url': enlace}
                return http('POST', url, headers=h, body=b'', timeout=300)
        except RuntimeError as e: errores.append(f'{prov}: {e}')  # el túnel no abrió: probar el siguiente
    raise ErrorAPI('no se pudo abrir ningún túnel -> ' + ' | '.join(errores))


def publicar(api, uid, item):
    video = (RAIZ / item['video']) if not Path(item['video']).is_absolute() else Path(item['video'])
    if not video.exists(): raise ErrorAPI(f'no existe el video {video}')
    cont = item.get('contenedor')
    if not cont:  # 1) crear contenedor de Reel  2) que Meta reciba el video (con reintentos, cada uno con contenedor nuevo)
        p = {'media_type': 'REELS', 'upload_type': 'resumable', 'caption': item['texto'], 'share_to_feed': 'true'}
        if item.get('portada_ms') is not None: p['thumb_offset'] = str(item['portada_ms'])
        modo = api.c.get('subida', 'tunel')
        fuente = video if api.sim else (version_hd(video) if modo == 'pages' else version_instagram(video) if modo == 'tunel' else video)
        liviana = None
        ultimo = None
        for intento in range(1, 4):
            if intento == 3 and modo == 'pages' and not api.sim:  # último envío: versión liviana de 720p, que Meta siempre acepta
                fuente = liviana = liviana or version_instagram(video)
            rc = api.post(f'{uid}/media', p); cont = rc['id']
            log(f"  contenedor {cont} (envío {intento}/3, modo {modo})")
            try:
                r = subir_video(api, cont, rc.get('uri'), fuente) if (modo in ('tunel', 'pages') and not api.sim) else api.subir(cont, fuente, rc.get('uri'))
                if not r.get('success', True): raise ErrorAPI(f'la subida falló: {r}')
                break
            except (ErrorAPI, RuntimeError) as e:
                ultimo = e; log(f'  el envío {intento} falló: {e}'); cont = None; time.sleep(5)
        if not cont: raise ErrorAPI(f'Meta no recibió el video tras 3 envíos: {ultimo}')
        item['contenedor'] = cont; item['estado'] = 'subiendo'  # solo se reanuda un contenedor con el video ya subido
        log('  video recibido por Meta, esperando a que Instagram lo procese…')
    if api.sim: estado = 'FINISHED'
    else:  # 3) esperar el procesamiento
        estado, t0 = 'IN_PROGRESS', time.time()
        while time.time() - t0 < 600:
            s = api.get(cont, {'fields': 'status_code,status'}); estado = s.get('status_code', '')
            if estado in ('FINISHED', 'PUBLISHED'): break
            if estado in ('ERROR', 'EXPIRED'): item.pop('contenedor', None); raise ErrorAPI(f"procesamiento {estado}: {s.get('status', '')}")
            time.sleep(10)
    if estado not in ('FINISHED', 'PUBLISHED'): raise ErrorAPI('Instagram sigue procesando; se reintentará en la próxima ejecución')
    med = api.post(f'{uid}/media_publish', {'creation_id': cont})['id']  # 4) publicar
    item['media_id'] = med; item['estado'] = 'publicado'; item['publicado'] = dt.datetime.now().strftime(FMT)
    item.pop('error', None)
    if not api.sim:
        try: item['permalink'] = api.get(med, {'fields': 'permalink'}).get('permalink')
        except ErrorAPI: pass
    log(f"  PUBLICADO #{item['id']} {item.get('permalink', '')}")


def comentar(api, item):
    """Deja los comentarios de item['comentarios'] en el reel publicado (p. ej. los de la opción A y B).
    Fijarlos no se puede por la API: se fijan a mano desde la app de Instagram."""
    hechos = item.setdefault('comentarios_ids', [])
    for txt in item.get('comentarios', [])[len(hechos):]:
        if api.sim: log(f'  [SIMULACIÓN] comentaría: {txt}'); continue
        r = api.post(f"{item['media_id']}/comments", {'message': txt})
        hechos.append(r['id']); log(f"  comentario {r['id']}: {txt}")
        time.sleep(3)


def comentarios_pendientes(cola):
    return [x for x in cola if x['estado'] == 'publicado' and x.get('media_id') and len(x.get('comentarios_ids', [])) < len(x.get('comentarios', [])) and x.get('comentar_intentos', 0) < 5]


def cmd_comentar(a):
    c = cargar_config(); api = API(c, a.simular); cola = leer(COLA, [])
    for item in comentarios_pendientes(cola):
        log(f"Comentando #{item['id']} {item.get('permalink', '')}")
        try: comentar(api, item); item.pop('error_comentario', None)
        except ErrorAPI as e:
            item['comentar_intentos'] = item.get('comentar_intentos', 0) + 1; item['error_comentario'] = str(e)
            log(f"  ERROR al comentar #{item['id']}: {e}")
        if not a.simular: guardar(COLA, cola)


def cmd_probar_subida(a):
    """Crea un contenedor y hace que Meta reciba el video, pero NO publica (el contenedor caduca solo en 24 h)."""
    c = cargar_config(); api = API(c); uid = user_id(api)
    modo = c.get('subida', 'tunel'); video = Path(a.video).resolve()
    video = version_hd(video) if modo == 'pages' else version_instagram(video)
    rc = api.post(f'{uid}/media', {'media_type': 'REELS', 'upload_type': 'resumable'}); cont = rc['id']
    log(f'PRUEBA (no publica): contenedor {cont}, video {video.name}')
    r = subir_video(api, cont, rc.get('uri'), video) if modo in ('tunel', 'pages') else api.subir(cont, video, rc.get('uri'))
    log(f'  respuesta de subida: {r}')
    for _ in range(30):
        st = api.get(cont, {'fields': 'status_code,status'})
        if st.get('status_code') in ('FINISHED', 'ERROR', 'EXPIRED'): break
        time.sleep(10)
    log(f"  PRUEBA estado final: {st.get('status_code')} - {st.get('status')}")
    if st.get('status_code') != 'FINISHED': sys.exit(1)


def cmd_ejecutar(a):
    c = cargar_config(); api = API(c, a.simular)
    ren = c.get('token_renovado')
    if not a.simular and c.get('api') != 'facebook' and ren and (dt.date.today() - dt.date.fromisoformat(ren)).days >= 30:
        try: cmd_renovar(a, c); api = API(cargar_config())
        except ErrorAPI as e: log(f'No se pudo renovar el token: {e}')
    cola = leer(COLA, []); ahora = dt.datetime.now()
    tocan = [x for x in cola if x['estado'] in ('pendiente', 'subiendo', 'error') and dt.datetime.strptime(x['publicar_en'], FMT) <= ahora and x.get('intentos', 0) < 5]
    if not tocan:
        if comentarios_pendientes(cola): cmd_comentar(a)
        else: log('Nada que publicar.' + (' (simulación)' if a.simular else ''))
        return
    uid = 'SIMULADO' if a.simular and not c.get('ig_user_id') else user_id(api)
    for item in sorted(tocan, key=lambda x: x['publicar_en'])[:a.max]:
        log(f"Publicando #{item['id']} ({Path(item['video']).name}) programado para {item['publicar_en']}" + (' [SIMULACIÓN]' if a.simular else ''))
        if a.simular:
            publicar(api, uid, json.loads(json.dumps(item))); continue  # no toca la cola
        try: publicar(api, uid, item)
        except ErrorAPI as e:
            item['intentos'] = item.get('intentos', 0) + 1; item['error'] = str(e)
            if item['estado'] != 'subiendo' or 'contenedor' not in item: item['estado'] = 'error'
            log(f"  ERROR #{item['id']} (intento {item['intentos']}/5): {e}")
        guardar(COLA, cola)
    if not a.simular and comentarios_pendientes(cola): cmd_comentar(a)


def main():
    ap = argparse.ArgumentParser(description='Publicador de Reels')
    sp = ap.add_subparsers(dest='cmd', required=True)
    sp.add_parser('verificar'); sp.add_parser('renovar'); sp.add_parser('cola')
    g = sp.add_parser('agregar'); g.add_argument('video'); g.add_argument('fecha', help='"AAAA-MM-DD HH:MM" hora local')
    g.add_argument('--texto'); g.add_argument('--texto-archivo'); g.add_argument('--portada-ms', type=int, default=None, help='ms del video para la portada')
    pr = sp.add_parser('probar-subida'); pr.add_argument('video')
    cm = sp.add_parser('comentar'); cm.add_argument('--simular', action='store_true')
    e = sp.add_parser('ejecutar'); e.add_argument('--simular', action='store_true'); e.add_argument('--max', type=int, default=1, help='máximo de reels por ejecución')
    a = ap.parse_args()
    try: {'verificar': cmd_verificar, 'renovar': cmd_renovar, 'agregar': cmd_agregar, 'cola': cmd_cola, 'ejecutar': cmd_ejecutar, 'probar-subida': cmd_probar_subida, 'comentar': cmd_comentar}[a.cmd](a)
    except ErrorAPI as e: log(f'ERROR: {e}'); sys.exit(1)


if __name__ == '__main__':
    main()
