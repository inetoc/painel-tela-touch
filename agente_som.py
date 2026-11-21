# -*- coding: utf-8 -*-
# Agente de som do computador oficial. So audio: listar dispositivos, escolher padrao, volume e mudo.
# Python padrao + modulo PowerShell AudioDeviceCmdlets. Escuta so em 127.0.0.1 e no IP do Tailscale.
import json, os, re, sys, time, hmac, secrets, threading, subprocess
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
