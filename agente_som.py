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


# ---------------- Destrava PC (le os numeros e aperta os botoes do proprio programa) ----------------
_ult = {'t': 0.0}
DESTRAVA_EXE = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'DestravaPC', 'DestravaPC.exe')
BOTOES = {'destravar': 'Destravar', 'lista': 'Ver lista'}

UIA_COMUM = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$p = Get-Process DestravaPC -ErrorAction SilentlyContinue | Select-Object -First 1
$raiz = [System.Windows.Automation.AutomationElement]::RootElement
"""

PS_LER = UIA_COMUM + r"""
if (-not $p) { '{"rodando":false}'; exit }
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $p.Id)
$t = @()
foreach ($j in $raiz.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
  foreach ($e in $j.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
    if ($e.Current.ControlType.ProgrammaticName -eq 'ControlType.Text') { $t += $e.Current.Name }
  }
}
[pscustomobject]@{ rodando = $true; textos = @($t) } | ConvertTo-Json -Compress
"""

PS_CLICAR = UIA_COMUM + r"""
$nome = '@@NOME@@'
if (-not $p) { 'FECHADO'; exit }
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $p.Id)
$cb = New-Object System.Windows.Automation.AndCondition((New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $nome)), (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
foreach ($j in $raiz.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)) {
  $b = $j.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cb)
  if ($b) { $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); 'OK'; exit }
}
'NAOACHEI'
"""


def ps_puro(cmd, timeout=30):
    p = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd],
                       capture_output=True, timeout=timeout, creationflags=NOWIN)
    return p.stdout.decode('utf-8', 'replace').strip(), p.stderr.decode('utf-8', 'replace').strip(), p.returncode


def _n(x):
    try:
        return float(str(x).replace('.', '').replace(',', '.')) if ',' in str(x) else float(x)
    except Exception:
        return None


def interpreta(textos):
    l1 = next((t for t in textos if re.match(r'^\s*CPU\s+\d+\s*%', t) and 'RAM' in t), None)
    l2 = next((t for t in textos if 'ferramentas por tr' in t), None)
    r = {'rodando': True, 'linha1': l1, 'linha2': l2, 'cpu': None, 'ram_pct': None, 'ram_livre': None, 'ferramentas': None, 'disco_livre': None}
    if l1:
        m = re.search(r'CPU\s+(\d+)\s*%', l1)
        r['cpu'] = int(m.group(1)) if m else None
        m = re.search(r'RAM\s+(\d+)\s*%', l1)
        r['ram_pct'] = int(m.group(1)) if m else None
        m = re.search(r'\(([\d.,]+)\s*GB livres\)', l1)
        r['ram_livre'] = _n(m.group(1)) if m else None
    if l2:
        m = re.search(r'(\d+)\s+ferramentas', l2)
        r['ferramentas'] = int(m.group(1)) if m else None
        m = re.search(r'Disco C:\s*([\d.,]+)\s*GB', l2)
        r['disco_livre'] = _n(m.group(1)) if m else None
    return r


def destrava_ler():
    out, err, rc = ps_puro(PS_LER)
    if rc != 0 or not out:
        raise RuntimeError('nao consegui ler o Destrava PC: ' + (err or out)[:200])
    j = json.loads(out.splitlines()[-1])
    if not j.get('rodando'):
        return {'rodando': False}
    t = j.get('textos') or []
    return interpreta(t if isinstance(t, list) else [t])


def destrava_botao(nome):
    if nome == 'abrir':
        if not os.path.exists(DESTRAVA_EXE):
            return {'ok': False, 'msg': 'nao achei o Destrava PC em ' + DESTRAVA_EXE}
        subprocess.Popen([DESTRAVA_EXE], close_fds=True)
        return {'ok': True, 'msg': 'abrindo o Destrava PC no computador'}
    rot = BOTOES.get(nome)
    if not rot:
        return {'ok': False, 'msg': 'botao desconhecido'}
    if nome == 'destravar':
        agora = time.time()
        if agora - _ult['t'] < 30:
            return {'ok': False, 'msg': 'espera uns segundos antes de destravar de novo'}
        _ult['t'] = agora
    antes = destrava_ler()
    if not antes.get('rodando'):
        return {'ok': False, 'msg': 'o Destrava PC esta fechado, abre ele primeiro'}
    out, err, rc = ps_puro(PS_CLICAR.replace('@@NOME@@', rot))
    r = out.splitlines()[-1] if out else ''
    log('destrava botao ' + nome + ' -> ' + r)
    if r != 'OK':
        return {'ok': False, 'msg': 'nao consegui apertar "' + rot + '" (' + (r or err[:80]) + ')'}
    if nome == 'lista':
        return {'ok': True, 'msg': 'lista aberta no computador'}
    time.sleep(4)
    depois = destrava_ler()
    msg = 'Destravar apertado'
    if antes.get('linha1') and depois.get('linha1'):
        msg += ' | antes: ' + antes['linha1'] + ' | depois: ' + depois['linha1']
    return {'ok': True, 'msg': msg, 'antes': antes, 'depois': depois}


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
            elif self.path.split('?')[0] == '/api/destrava':
                self._send(200, destrava_ler())
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
            elif r == '/api/destrava/botao':
                self._send(200, destrava_botao(str(d.get('botao', ''))))
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
