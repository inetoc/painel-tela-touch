# -*- coding: utf-8 -*-
# Agente de som do computador oficial. So audio: listar dispositivos, escolher padrao, volume e mudo.
# Python padrao + modulo PowerShell AudioDeviceCmdlets. Escuta so em 127.0.0.1 e no IP do Tailscale.
import json, os, re, sys, time, hmac, secrets, threading, subprocess, shutil, ctypes
import ctypes.wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = 8778
NOWIN = 0x08000000
TS_EXE = r'C:\Program Files\Tailscale\tailscale.exe'
TOKEN_FILE = os.path.join(BASE, 'token.txt')
LOG_FILE = os.path.join(BASE, 'agente.log')

if not os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, 'w') as f:
        f.write(secrets.token_urlsafe(12))
with open(TOKEN_FILE) as f:
    TOKEN = f.read().strip()


def log(msg):
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + str(msg) + '\n')
    except Exception:
        pass


def ps(cmd, timeout=25):
    p = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Import-Module AudioDeviceCmdlets; " + cmd],
                       capture_output=True, timeout=timeout, creationflags=NOWIN)
    return p.stdout.decode('utf-8', 'replace').strip(), p.stderr.decode('utf-8', 'replace').strip(), p.returncode


PS_LISTA = r"""
$d = @(Get-AudioDevice -List | ForEach-Object { [pscustomobject]@{ id = $_.ID; nome = $_.Name; tipo = [string]$_.Type; padrao = [bool]$_.Default; comunic = [bool]$_.DefaultCommunication } })
[pscustomobject]@{ dispositivos = $d; pv = [string](Get-AudioDevice -PlaybackVolume); pm = [bool](Get-AudioDevice -PlaybackMute); rv = [string](Get-AudioDevice -RecordingVolume); rm = [bool](Get-AudioDevice -RecordingMute) } | ConvertTo-Json -Compress -Depth 4
"""


def num(x):
    try:
        return float(str(x).replace('%', '').replace(',', '.').strip())
    except Exception:
        return None


def dispositivos():
    out, err, rc = ps(PS_LISTA)
    if rc != 0 or not out:
        raise RuntimeError('nao consegui listar: ' + (err or out)[:200])
    j = json.loads(out.splitlines()[-1])
    return {'dispositivos': j['dispositivos'], 'saida': {'volume': num(j['pv']), 'mudo': j['pm']},
            'entrada': {'volume': num(j['rv']), 'mudo': j['rm']}}


ID_OK = re.compile(r'^[A-Za-z0-9{}.\-]{5,120}$')


def padrao(d):
    i = str(d.get('id', ''))
    if not ID_OK.match(i):
        return {'ok': False, 'msg': 'id invalido'}
    out, err, rc = ps("Set-AudioDevice -ID '" + i + "' | Out-Null")
    log('padrao ' + i + ' rc=' + str(rc))
    return {'ok': rc == 0, 'msg': 'dispositivo padrao trocado' if rc == 0 else 'falhou: ' + (err or out)[:150]}


def volume(d):
    t = d.get('tipo')
    n = max(0, min(100, int(d.get('n', 50))))
    if t not in ('saida', 'entrada'):
        return {'ok': False, 'msg': 'tipo invalido'}
    out, err, rc = ps('Set-AudioDevice -' + ('PlaybackVolume ' if t == 'saida' else 'RecordingVolume ') + str(n))
    return {'ok': rc == 0, 'msg': 'volume ' + str(n) + '%' if rc == 0 else 'falhou: ' + (err or out)[:150]}


def mudo(d):
    t = d.get('tipo')
    if t not in ('saida', 'entrada'):
        return {'ok': False, 'msg': 'tipo invalido'}
    v = '$true' if d.get('on') else '$false'
    out, err, rc = ps('Set-AudioDevice -' + ('PlaybackMute ' if t == 'saida' else 'RecordingMute ') + v)
    return {'ok': rc == 0, 'msg': ('mudo' if d.get('on') else 'som ligado') if rc == 0 else 'falhou: ' + (err or out)[:150]}


# ---------------- status do computador e limpeza segura ----------------
_cpu = {'v': None}
_ult = {'t': 0.0}
DESTRAVAR_CFG = os.path.join(BASE, 'destravar.json')
# nunca encerrados, mesmo que estejam na lista do usuario
PROTEGIDOS = {'sunshine', 'sunshinesvc', 'tailscaled', 'tailscale-ipn', 'tailscale', 'explorer', 'destravapc', 'system',
              'svchost', 'winlogon', 'csrss', 'lsass', 'services', 'wininit', 'dwm', 'smss', 'pythonw', 'python', 'powershell'}
NOME_OK = re.compile(r'^[A-Za-z0-9._\- ]{1,60}$')


class _FT(ctypes.Structure):
    _fields_ = [('lo', ctypes.wintypes.DWORD), ('hi', ctypes.wintypes.DWORD)]


class _MEM(ctypes.Structure):
    _fields_ = [('len', ctypes.wintypes.DWORD), ('load', ctypes.wintypes.DWORD),
                ('tot', ctypes.c_ulonglong), ('av', ctypes.c_ulonglong),
                ('ptot', ctypes.c_ulonglong), ('pav', ctypes.c_ulonglong),
                ('vtot', ctypes.c_ulonglong), ('vav', ctypes.c_ulonglong), ('ext', ctypes.c_ulonglong)]


def pc():
    k = ctypes.windll.kernel32
    i, ke, us = _FT(), _FT(), _FT()
    k.GetSystemTimes(ctypes.byref(i), ctypes.byref(ke), ctypes.byref(us))
    idle = (i.hi << 32) | i.lo
    tot = ((ke.hi << 32) | ke.lo) + ((us.hi << 32) | us.lo)
    ult = _cpu['v']
    _cpu['v'] = (idle, tot)
    cpu = None
    if ult and tot - ult[1] > 0:
        cpu = max(0, min(100, round(100 * (1 - (idle - ult[0]) / (tot - ult[1])))))
    m = _MEM()
    m.len = ctypes.sizeof(m)
    k.GlobalMemoryStatusEx(ctypes.byref(m))
    try:
        d = shutil.disk_usage('C:\\')
        disco = {'livre': round(d.free / 2**30, 1), 'total': round(d.total / 2**30)}
    except Exception:
        disco = None
    return {'cpu': cpu, 'ram_pct': m.load, 'ram_livre': round(m.av / 2**30, 1), 'ram_total': round(m.tot / 2**30, 1), 'disco': disco}


def destravar_cfg():
    try:
        with open(DESTRAVAR_CFG, encoding='utf-8') as f:
            c = json.load(f)
    except Exception:
        c = {}
    enc = [str(x).strip() for x in (c.get('encerrar') or []) if isinstance(x, str)]
    cmd = c.get('comando') if isinstance(c.get('comando'), list) else None
    return {'encerrar': enc[:30], 'comando': [str(x) for x in cmd][:10] if cmd else None}


def liberar_memoria():
    k, pa = ctypes.windll.kernel32, ctypes.windll.psapi
    k.OpenProcess.restype = ctypes.c_void_p
    arr = (ctypes.wintypes.DWORD * 4096)()
    n = ctypes.wintypes.DWORD()
    pa.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(n))
    cont = 0
    for pid in list(arr)[:n.value // 4]:
        if not pid:
            continue
        h = k.OpenProcess(0x0100 | 0x0400, False, pid)  # PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION
        if h:
            if pa.EmptyWorkingSet(ctypes.c_void_p(h)):
                cont += 1
            k.CloseHandle(ctypes.c_void_p(h))
    return cont


def encerrar_lista(nomes):
    feitos, recusados = [], []
    for nome in nomes:
        base = nome.lower()
        if base.endswith('.exe'):
            base = base[:-4]
        if not NOME_OK.match(nome) or base in PROTEGIDOS:
            recusados.append(nome)
            continue
        r = subprocess.run(['taskkill', '/IM', nome if nome.lower().endswith('.exe') else nome + '.exe', '/F'],
                           capture_output=True, creationflags=NOWIN, timeout=20)
        if r.returncode == 0:
            feitos.append(nome)
    return feitos, recusados


def destravar():
    agora = time.time()
    if agora - _ult['t'] < 30:
        return {'ok': False, 'msg': 'espera uns segundos antes de destravar de novo'}
    _ult['t'] = agora
    antes = pc()
    cfg = destravar_cfg()
    extra = ''
    if cfg['comando']:
        try:
            r = subprocess.run(cfg['comando'], capture_output=True, creationflags=NOWIN, timeout=120)
            extra = ' | comando externo: codigo ' + str(r.returncode)
        except Exception as e:
            extra = ' | comando externo falhou: ' + str(e)[:80]
    feitos, recusados = encerrar_lista(cfg['encerrar'])
    n = liberar_memoria()
    time.sleep(1.5)
    depois = pc()
    log('destravar: ram ' + str(antes['ram_pct']) + '% -> ' + str(depois['ram_pct']) + '%, encerrados ' + str(feitos) + ', recusados ' + str(recusados))
    msg = 'RAM ' + str(antes['ram_pct']) + '% -> ' + str(depois['ram_pct']) + '% (' + str(antes['ram_livre']) + ' -> ' + str(depois['ram_livre']) + ' GB livres)'
    if feitos:
        msg += ' | encerrados: ' + ', '.join(feitos)
    if recusados:
        msg += ' | protegidos/ignorados: ' + ', '.join(recusados)
    return {'ok': True, 'msg': msg + extra, 'antes': antes, 'depois': depois, 'processos_aliviados': n, 'encerrados': feitos}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        b = json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def _auth(self):
        ok = hmac.compare_digest(self.headers.get('X-Token', '').encode('utf-8'), TOKEN.encode('utf-8'))
        if not ok:
            time.sleep(1)
        return ok

    def do_GET(self):
        if not self._auth():
            return self._send(401, {'erro': 'token errado'})
        try:
            if self.path.split('?')[0] == '/api/dispositivos':
                self._send(200, dispositivos())
            elif self.path.split('?')[0] == '/api/pc':
                self._send(200, pc())
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
            r = self.path.split('?')[0]
            if r == '/api/padrao':
                self._send(200, padrao(d))
            elif r == '/api/volume':
                self._send(200, volume(d))
            elif r == '/api/mudo':
                self._send(200, mudo(d))
            elif r == '/api/destravar':
                self._send(200, destravar())
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
    threading.Thread(target=vigiar_tailscale, daemon=True).start()
    servir('127.0.0.1')
