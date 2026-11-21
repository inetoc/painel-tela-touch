# -*- coding: utf-8 -*-
# Painel do Notebook: servidor local (so Python padrao). Escuta em 127.0.0.1 e no IP do Tailscale.
import json, os, re, sys, time, hmac, atexit, secrets, threading, subprocess, tempfile, ctypes
import ctypes.wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8777
NOWIN = 0x08000000
TS_EXE = r'C:\Program Files\Tailscale\tailscale.exe'
TOKEN_FILE = os.path.join(BASE, 'token.txt')
LOG_FILE = os.path.join(BASE, 'painel.log')

if not os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, 'w') as f:
        f.write(secrets.token_urlsafe(9))
with open(TOKEN_FILE) as f:
    TOKEN = f.read().strip()


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + str(msg) + '\n')
    except Exception:
        pass


def run_ps(script, timeout=120):
    prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n$ProgressPreference = 'SilentlyContinue'\n"
    fd, path = tempfile.mkstemp(suffix='.ps1', dir=BASE)
    os.close(fd)
    try:
        with open(path, 'w', encoding='utf-8-sig') as f:
            f.write(prefix + script)
        p = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', path],
            capture_output=True, timeout=timeout, creationflags=NOWIN)
        out = p.stdout.decode('utf-8', 'replace') + p.stderr.decode('utf-8', 'replace')
        return out, p.returncode
    except subprocess.TimeoutExpired:
        return 'ERRO: passou do tempo limite (' + str(timeout) + 's)', 124
    except Exception as e:
        return 'ERRO: ' + str(e), 1
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


PS_PEGA = r"""
function Pega($tipo) {
  if ($tipo -eq 'touchscreen') {
    Get-PnpDevice -PresentOnly | Where-Object { ($_.Class -eq 'Mouse' -or $_.Class -eq 'HIDClass') -and $_.FriendlyName -match 'touchscreen|touch screen|tela sens' }
  } else {
    Get-PnpDevice -PresentOnly | Where-Object { ($_.Class -eq 'Mouse' -or $_.Class -eq 'HIDClass') -and $_.FriendlyName -match 'touchpad|synaptics|elan|i2c' -and $_.FriendlyName -notmatch 'touchscreen|touch screen|tela sens' }
  }
}
"""

PS_STATUS = PS_PEGA + r"""
function St($d) {
  $d = @($d)
  if ($d.Count -eq 0) { return 'none' }
  if (@($d | Where-Object { $_.Status -eq 'OK' }).Count -gt 0) { return 'on' }
  return 'off'
}
@{ touchscreen = (St (Pega 'touchscreen')); touchpad = (St (Pega 'touchpad')) } | ConvertTo-Json -Compress
"""

PS_TOGGLE = PS_PEGA + r"""
$devs = @(Pega 'TIPO_AQUI')
if ($devs.Count -eq 0) { 'ERRO: nao encontrei esse dispositivo (usa a lista avancada)'; exit 2 }
$ligado = @($devs | Where-Object { $_.Status -eq 'OK' }).Count -gt 0
if ($ligado) {
  $ids = @($devs | ForEach-Object { $_.InstanceId })
  $outros = @(Get-PnpDevice -Class Mouse -PresentOnly | Where-Object { $_.Status -eq 'OK' -and ($ids -notcontains $_.InstanceId) })
  if ($outros.Count -eq 0) { 'ERRO: nao posso, ficaria sem nenhum mouse/toque'; exit 3 }
  foreach ($d in $devs) { Disable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop }
  'desligado'
} else {
  foreach ($d in $devs) { Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false -ErrorAction Stop }
  'ligado'
}
"""

PS_REATIVAR = PS_PEGA + r"""
foreach ($t in 'touchscreen', 'touchpad') {
  foreach ($d in @(Pega $t)) { Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false }
}
'tudo reativado'
"""

PS_DEVS = r"""
$l = @(Get-PnpDevice -PresentOnly | Where-Object { $_.Class -eq 'Keyboard' -or $_.Class -eq 'Mouse' -or $_.Class -eq 'HIDClass' } | Select-Object Status, Class, FriendlyName, InstanceId | Sort-Object Class, FriendlyName)
ConvertTo-Json -InputObject $l -Compress
"""

PS_DEV_TOGGLE = r"""
$id = 'ID_AQUI'
$d = Get-PnpDevice -InstanceId $id
if (-not $d) { 'ERRO: nao achei esse dispositivo'; exit 2 }
if ($d.Status -eq 'OK') { Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop; 'desligado' }
else { Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop; 'ligado' }
"""

PS_MATAR_HOOKS = r"""
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*painel*hook.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
"""

_cache = {'t': 0, 'v': None}
_lock = threading.Lock()
_hook = {'p': None}


def hook_ativo():
    return _hook['p'] is not None and _hook['p'].poll() is None


def hook_toggle():
    if hook_ativo():
        _hook['p'].kill()
        _hook['p'] = None
        return 'teclado liberado'
    _hook['p'] = subprocess.Popen(
        ['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
         '-File', os.path.join(BASE, 'hook.ps1'), str(os.getpid())],
        creationflags=NOWIN)
    return 'bloqueando o teclado fisico (leva uns 3 segundos)'


def nunca_dormir():
    try:
        out = subprocess.run(['powercfg', '/q', 'SCHEME_CURRENT', 'SUB_SLEEP', 'STANDBYIDLE'],
                             capture_output=True, creationflags=NOWIN).stdout.decode('utf-8', 'replace')
        vals = re.findall(r'0x[0-9a-fA-F]{8}', out)
        return len(vals) >= 2 and int(vals[-2], 16) == 0
    except Exception:
        return False


def status():
    with _lock:
        if _cache['v'] is not None and time.time() - _cache['t'] < 8:
            v = dict(_cache['v'])
        else:
            out, rc = run_ps(PS_STATUS, 40)
            try:
                v = json.loads([x for x in out.strip().splitlines() if x.strip()][-1])
            except Exception:
                v = {'touchscreen': 'none', 'touchpad': 'none'}
            _cache['t'] = time.time()
            _cache['v'] = dict(v)
    v['teclado_bloqueado'] = hook_ativo()
    v['nunca_dormir'] = nunca_dormir()
    return v


def invalida():
    _cache['t'] = 0


def toggle(tipo):
    if tipo not in ('touchscreen', 'touchpad'):
        return {'ok': False, 'msg': 'tipo invalido'}
    out, rc = run_ps(PS_TOGGLE.replace('TIPO_AQUI', tipo), 60)
    invalida()
    return {'ok': rc == 0, 'msg': out.strip()}


def dev_toggle(dev_id):
    if not dev_id:
        return {'ok': False, 'msg': 'sem id'}
    out, rc = run_ps(PS_DEV_TOGGLE.replace('ID_AQUI', dev_id.replace("'", "''")), 60)
    invalida()
    return {'ok': rc == 0, 'msg': out.strip()}


REPO = 'inetoc/painel-tela-touch'
ARQS = ('server.py', 'index.html', 'hook.ps1')


def atualizar():
    import urllib.request
    novos = {}
    for a in ARQS:
        rq = urllib.request.Request('https://api.github.com/repos/' + REPO + '/contents/' + a,
                                    headers={'Accept': 'application/vnd.github.raw', 'User-Agent': 'painel-tela-touch'})
        with urllib.request.urlopen(rq, timeout=25) as r:
            novos[a] = r.read()
        if len(novos[a]) < 200:
            return {'ok': False, 'msg': 'arquivo vazio/estranho: ' + a}
    try:
        compile(novos['server.py'].decode('utf-8'), 'server.py', 'exec')
    except Exception as e:
        return {'ok': False, 'msg': 'server.py novo com erro, nao apliquei: ' + str(e)}
    for a in ARQS:
        dest = os.path.join(BASE, a)
        if os.path.exists(dest):
            try:
                os.replace(dest, dest + '.bak')
            except Exception:
                pass
        with open(dest, 'wb') as f:
            f.write(novos[a])
    log('atualizado do GitHub, reiniciando')
    cmd = 'ping -n 4 127.0.0.1 >nul & "' + sys.executable + '" "' + os.path.join(BASE, 'server.py') + '"'
    subprocess.Popen(['cmd', '/c', cmd], creationflags=0x00000008 | 0x00000200 | NOWIN, close_fds=True)
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {'ok': True, 'msg': 'atualizado, reiniciando o painel...'}


def acao(nome):
    if nome == 'teclado':
        return {'ok': True, 'msg': hook_toggle()}
    if nome == 'osk':
        subprocess.Popen(['osk.exe'])
        return {'ok': True, 'msg': 'teclado na tela aberto'}
    if nome == 'atualizar':
        try:
            return atualizar()
        except Exception as e:
            return {'ok': False, 'msg': 'falhou: ' + str(e)}
    if nome == 'abrir_tela':
        subprocess.Popen('start "" "http://127.0.0.1:' + str(PORT) + '/tela#' + TOKEN + '"', shell=True, creationflags=NOWIN)
        return {'ok': True, 'msg': 'tela de texto aberta na tela touch (duplo toque = tela cheia)'}
    if nome == 'tela_off':
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
        return {'ok': True, 'msg': 'tela apagada (mexer no mouse acorda)'}
    if nome == 'sono':
        if nunca_dormir():
            subprocess.run(['powercfg', '/change', 'standby-timeout-ac', '30'], creationflags=NOWIN)
            msg = 'voltou a dormir depois de 30 min'
        else:
            subprocess.run(['powercfg', '/change', 'standby-timeout-ac', '0'], creationflags=NOWIN)
            subprocess.run(['powercfg', '/change', 'hibernate-timeout-ac', '0'], creationflags=NOWIN)
            msg = 'nunca mais dorme na tomada'
        subprocess.run(['powercfg', '/setactive', 'SCHEME_CURRENT'], creationflags=NOWIN)
        return {'ok': True, 'msg': msg}
    if nome == 'reativar':
        if hook_ativo():
            hook_toggle()
        out, rc = run_ps(PS_REATIVAR, 60)
        invalida()
        return {'ok': rc == 0, 'msg': 'teclado liberado. ' + out.strip()}
    if nome == 'reiniciar':
        subprocess.Popen(['shutdown', '/r', '/t', '3'], creationflags=NOWIN)
        return {'ok': True, 'msg': 'reiniciando em 3s'}
    if nome == 'desligar':
        subprocess.Popen(['shutdown', '/s', '/t', '3'], creationflags=NOWIN)
        return {'ok': True, 'msg': 'desligando em 3s'}
    return {'ok': False, 'msg': 'acao desconhecida'}


MSG = {'t': ''}


def mouse(d):
    u = ctypes.windll.user32
    ev = u.mouse_event
    if 'dx' in d or 'dy' in d:
        dx = max(-2000, min(2000, int(d.get('dx', 0))))
        dy = max(-2000, min(2000, int(d.get('dy', 0))))
        pt = ctypes.wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetCursorPos(pt.x + dx, pt.y + dy)
    c = d.get('click')
    if c in ('left', 'double'):
        for _ in range(2 if c == 'double' else 1):
            ev(0x0002, 0, 0, 0, 0)
            ev(0x0004, 0, 0, 0, 0)
    elif c == 'right':
        ev(0x0008, 0, 0, 0, 0)
        ev(0x0010, 0, 0, 0, 0)
    elif c == 'down':
        ev(0x0002, 0, 0, 0, 0)
    elif c == 'up':
        ev(0x0004, 0, 0, 0, 0)
    if 'scroll' in d:
        n = max(-30, min(30, int(d['scroll'])))
        ev(0x0800, 0, 0, (n * 120) & 0xFFFFFFFF, 0)
    return {'ok': True}


TELA_HTML = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tela touch</title>
<style>html,body{height:100%;margin:0;background:#0f0d1c;color:#ece9ff;font-family:system-ui,"Segoe UI",sans-serif}
#t{box-sizing:border-box;min-height:100%;padding:4vh 5vw;font-size:5vw;line-height:1.3;white-space:pre-wrap;word-break:break-word;display:flex;align-items:center}
#t span{display:block;width:100%}#v{color:#6a6694;font-size:3vw}</style></head><body><div id="t"><span id="v">esperando texto...</span></div>
<script>
var TK='';try{TK=localStorage.getItem('tk')||''}catch(e){}
if(location.hash.length>1){TK=decodeURIComponent(location.hash.slice(1));try{localStorage.setItem('tk',TK)}catch(e){}history.replaceState(null,'',location.pathname)}
var ult=null;
function ler(){fetch('/api/texto',{headers:{'X-Token':TK},cache:'no-store'}).then(function(r){return r.json()}).then(function(j){
 if(j.erro){document.getElementById('v').textContent='sem permissao (token). Abre de novo pelo card Tela de texto.';return}if(j.t===undefined||j.t===ult)return;ult=j.t;var e=document.getElementById('v');e.textContent=j.t||'(vazio)';e.style.color=j.t?'#ece9ff':'#6a6694';e.style.fontSize=j.t&&j.t.length>200?'3vw':(j.t&&j.t.length>80?'4vw':'5vw')}).catch(function(){document.getElementById('v').textContent='sem conexao com o painel...'})}
setInterval(ler,1000);ler();
document.addEventListener('dblclick',function(){if(document.fullscreenElement)document.exitFullscreen();else document.documentElement.requestFullscreen()});
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        t = self.headers.get('X-Token', '')
        ok = hmac.compare_digest(t.encode('utf-8'), TOKEN.encode('utf-8'))
        if not ok:
            time.sleep(1)
        return ok

    def do_GET(self):
        rota = self.path.split('?')[0]
        try:
            if rota in ('/', '/index.html'):
                with open(os.path.join(BASE, 'index.html'), 'rb') as f:
                    self._send(200, f.read(), 'text/html')
            elif rota == '/tela':
                self._send(200, TELA_HTML, 'text/html')
            elif rota == '/api/texto':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, {'t': MSG['t']})
            elif rota == '/api/status':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, status())
            elif rota == '/api/devs':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                out, rc = run_ps(PS_DEVS, 40)
                try:
                    lista = json.loads(out.strip().splitlines()[-1])
                    if isinstance(lista, dict):
                        lista = [lista]
                except Exception:
                    lista = []
                self._send(200, lista)
            else:
                self._send(404, {'erro': 'nao existe'})
        except Exception as e:
            log('GET erro: ' + str(e))
            self._send(500, {'erro': str(e)})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {'erro': 'token errado'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
            d = json.loads(self.rfile.read(n) or b'{}')
            rota = self.path.split('?')[0]
            if rota == '/api/run':
                t0 = time.time()
                out, rc = run_ps(d.get('script', ''), 300)
                self._send(200, {'saida': out, 'codigo': rc, 'seg': round(time.time() - t0, 1)})
            elif rota == '/api/texto':
                MSG['t'] = str(d.get('t', ''))[:5000]
                self._send(200, {'ok': True})
            elif rota == '/api/toggle':
                self._send(200, toggle(d.get('tipo')))
            elif rota == '/api/acao':
                self._send(200, acao(d.get('nome')))
            elif rota == '/api/mouse':
                self._send(200, mouse(d))
            elif rota == '/api/dev':
                self._send(200, dev_toggle(d.get('id', '')))
            else:
                self._send(404, {'erro': 'nao existe'})
        except Exception as e:
            log('POST erro: ' + str(e))
            self._send(500, {'erro': str(e)})


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def servir(ip):
    try:
        log('escutando em ' + ip + ':' + str(PORT))
        Servidor((ip, PORT), H).serve_forever()
    except Exception as e:
        log('falhou em ' + ip + ': ' + str(e))


def ts_ip():
    try:
        r = subprocess.run([TS_EXE, 'ip', '-4'], capture_output=True, timeout=10, creationflags=NOWIN)
        linhas = r.stdout.decode('utf-8', 'replace').strip().splitlines()
        ip = linhas[0].strip() if linhas else ''
        return ip if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip) else None
    except Exception:
        return None


def vigiar_tailscale():
    while True:
        ip = ts_ip()
        if ip:
            servir(ip)
        time.sleep(10)


if __name__ == '__main__':
    log('iniciando')
    run_ps(PS_MATAR_HOOKS, 30)
    atexit.register(lambda: _hook['p'] and _hook['p'].poll() is None and _hook['p'].kill())
    threading.Thread(target=vigiar_tailscale, daemon=True).start()
    servir('127.0.0.1')
