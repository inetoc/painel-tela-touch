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


def relancar():
    # O Agendador mata tudo que o painel inicia quando a tarefa termina. Entao o reinicio roda
    # numa tarefa propria (PainelReinicio), fora dessa arvore: ela encerra e liga a PainelNotebook.
    cmd = ('cmd /c ping -n 3 127.0.0.1 >nul & schtasks /end /tn PainelNotebook'
           ' & ping -n 3 127.0.0.1 >nul & schtasks /run /tn PainelNotebook')
    try:
        r = subprocess.run(['schtasks', '/create', '/tn', 'PainelReinicio', '/tr', cmd, '/sc', 'once',
                            '/st', '00:00', '/rl', 'HIGHEST', '/f'], capture_output=True, creationflags=NOWIN, timeout=30)
        if r.returncode != 0:
            return False, 'nao criei a tarefa de reinicio: ' + (r.stdout + r.stderr).decode('cp850', 'replace').strip()[:200]
        r = subprocess.run(['schtasks', '/run', '/tn', 'PainelReinicio'], capture_output=True, creationflags=NOWIN, timeout=30)
        if r.returncode != 0:
            return False, 'nao consegui rodar a tarefa de reinicio: ' + (r.stdout + r.stderr).decode('cp850', 'replace').strip()[:200]
        return True, ''
    except Exception as e:
        return False, 'erro no reinicio: ' + str(e)


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
    ok, erro = relancar()
    if not ok:
        log(erro)
        return {'ok': False, 'msg': 'arquivos atualizados, mas nao reiniciou: ' + erro}
    return {'ok': True, 'msg': 'atualizado, reiniciando o painel...'}


def acao(nome):
    if nome == 'teclado':
        return {'ok': True, 'msg': hook_toggle()}
    if nome == 'reiniciar_painel':
        log('reiniciar_painel pedido')
        ok, erro = relancar()
        return {'ok': ok, 'msg': 'reiniciando o painel (uns 8 segundos)...' if ok else erro}
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
    if nome == 'abrir_kiosk':
        subprocess.Popen('start "" "http://127.0.0.1:' + str(PORT) + '/kiosk#' + TOKEN + '"', shell=True, creationflags=NOWIN)
        return {'ok': True, 'msg': 'kiosk aberto na tela touch (duplo toque = tela cheia)'}
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


KIOSK_HTML = r"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Kiosk</title>
<style>
html,body{height:100%;margin:0;background:radial-gradient(circle at 30% 20%,#2a1f5c,#0f0d1c 60%);color:#ece9ff;font-family:system-ui,"Segoe UI",sans-serif;overflow:hidden}
#w{box-sizing:border-box;height:100%;padding:3vh 4vw;display:flex;flex-direction:column;gap:2vh}
#top{display:flex;justify-content:space-between;align-items:flex-start}
#hora{font-size:16vh;font-weight:800;line-height:1;letter-spacing:-.02em}
#data{font-size:3.4vh;color:#b9b4e6;margin-top:1vh;text-transform:capitalize}
#clima{text-align:right;font-size:3.4vh;color:#b9b4e6}#clima .t{font-size:9vh;font-weight:700;color:#ece9ff}
#msg{display:none;background:linear-gradient(135deg,#6e46c8,#9b6bff);border-radius:2vh;padding:2.4vh 3vw;font-size:4.6vh;font-weight:700;white-space:pre-wrap;word-break:break-word}
#proj{display:grid;grid-template-columns:repeat(auto-fill,minmax(22vw,1fr));gap:2vh;margin-top:auto}
.p{background:#1a1733cc;border:1px solid #2e2a5a;border-radius:2vh;padding:2vh 2vw;font-size:3vh;display:flex;gap:1.2vw;align-items:center}
.p small{display:block;color:#9a96c4;font-size:2vh}
.p.off{border-color:#aa3246;background:#3a1420cc}
</style></head><body><div id="w">
<div id="top"><div><div id="hora">--:--</div><div id="data"></div></div><div id="clima"></div></div>
<div id="msg"></div>
<div id="proj"></div>
</div>
<script>
var TK='';try{TK=localStorage.getItem('tk')||''}catch(e){}
if(location.hash.length>1){TK=decodeURIComponent(location.hash.slice(1));try{localStorage.setItem('tk',TK)}catch(e){}history.replaceState(null,'',location.pathname)}
function $(i){return document.getElementById(i)}
function H(u){return fetch(u,{headers:{'X-Token':TK},cache:'no-store'}).then(function(r){return r.json()})}
function tick(){var d=new Date();$('hora').textContent=d.toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});$('data').textContent=d.toLocaleDateString('pt-BR',{weekday:'long',day:'numeric',month:'long'})}
setInterval(tick,1000);tick();
function emo(c){var n=c===0?[0x2600,0xFE0F]:c<=2?[0x1F324,0xFE0F]:c===3?[0x2601,0xFE0F]:c<=48?[0x1F32B,0xFE0F]:c<=57?[0x1F326,0xFE0F]:c<=67?[0x1F327,0xFE0F]:c<=77?[0x2744,0xFE0F]:c<=82?[0x1F327,0xFE0F]:[0x26C8,0xFE0F];return String.fromCodePoint.apply(null,n)}
function clima(){H('/api/kioskcfg').then(function(c){if(c.lat==null||c.lon==null){$('clima').textContent='';return}
fetch('https://api.open-meteo.com/v1/forecast?latitude='+c.lat+'&longitude='+c.lon+'&current=temperature_2m,weather_code&timezone=auto').then(function(r){return r.json()}).then(function(j){var k=j.current;$('clima').innerHTML='<div class="t">'+emo(k.weather_code)+' '+Math.round(k.temperature_2m)+'&deg;</div>'+(c.cidade||'')}).catch(function(){})}).catch(function(){})}
clima();setInterval(clima,600000);
function proj(){H('/api/projetos').then(function(l){if(!Array.isArray(l))return;$('proj').innerHTML=l.map(function(p){return '<div class="p'+(p.ok?'':' off')+'"><span style="font-size:5vh">'+(p.ok?'&#128994;':'&#128308;')+'</span><div>'+p.nome.replace(/</g,'&lt;')+'<small>'+(p.ok?p.ms+' ms':'FORA DO AR')+'</small></div></div>'}).join('')}).catch(function(){})}
proj();setInterval(proj,30000);
function txt(){H('/api/texto').then(function(j){var m=$('msg');if(j.t){m.style.display='block';m.textContent=j.t}else m.style.display='none'}).catch(function(){})}
txt();setInterval(txt,2000);
document.addEventListener('dblclick',function(){if(document.fullscreenElement)document.exitFullscreen();else document.documentElement.requestFullscreen()});
</script></body></html>
"""
MSG = {'t': ''}
_cpu = {'v': None}


class _FT(ctypes.Structure):
    _fields_ = [('lo', ctypes.wintypes.DWORD), ('hi', ctypes.wintypes.DWORD)]


class _MEM(ctypes.Structure):
    _fields_ = [('len', ctypes.wintypes.DWORD), ('load', ctypes.wintypes.DWORD),
                ('tot', ctypes.c_ulonglong), ('av', ctypes.c_ulonglong),
                ('ptot', ctypes.c_ulonglong), ('pav', ctypes.c_ulonglong),
                ('vtot', ctypes.c_ulonglong), ('vav', ctypes.c_ulonglong), ('ext', ctypes.c_ulonglong)]


def sistema():
    import shutil
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
    discos = []
    for L in 'CD':
        try:
            d = shutil.disk_usage(L + ':\\')
            discos.append({'l': L, 'livre': round(d.free / 2**30), 'total': round(d.total / 2**30)})
        except Exception:
            pass
    k.GetTickCount64.restype = ctypes.c_ulonglong
    return {'cpu': cpu, 'ram_pct': m.load, 'ram_livre': round(m.av / 2**30, 1), 'ram_total': round(m.tot / 2**30, 1),
            'discos': discos, 'uptime_min': int(k.GetTickCount64() / 60000)}


_VK = {'vol_up': 0xAF, 'vol_down': 0xAE, 'mute': 0xAD, 'play': 0xB3, 'next': 0xB0, 'prev': 0xB1}


def tecla(d):
    vk = _VK.get(d.get('k'))
    if not vk:
        return {'ok': False, 'msg': 'tecla desconhecida'}
    for _ in range(max(1, min(5, int(d.get('n', 1))))):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
    return {'ok': True}


def brilho(d):
    n = max(0, min(100, int(d.get('n', 50))))
    out, rc = run_ps("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1, " + str(n) + ")", 20)
    return {'ok': rc == 0, 'msg': ('brilho ' + str(n) + '%') if rc == 0 else ('nao deu: ' + out.strip()[:120])}


PS_SHOT = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();' -Name D -Namespace W
[W.D]::SetProcessDPIAware() | Out-Null
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('@@P@@', [System.Drawing.Imaging.ImageFormat]::Jpeg)
"""


def shot():
    fd, path = tempfile.mkstemp(suffix='.jpg', dir=BASE)
    os.close(fd)
    try:
        out, rc = run_ps(PS_SHOT.replace('@@P@@', path), 30)
        with open(path, 'rb') as f:
            data = f.read()
        if len(data) < 500:
            raise RuntimeError('captura vazia: ' + out.strip()[:200])
        return data
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


RECEBIDOS = os.path.join(os.path.dirname(BASE), 'recebidos')


def abrir_url(d):
    u = str(d.get('u', '')).strip()
    if not re.match(r'^https?://[^\s]+$', u) or len(u) > 2000:
        return {'ok': False, 'msg': 'link invalido (precisa comecar com http:// ou https://)'}
    os.startfile(u)
    return {'ok': True, 'msg': 'link aberto na tela touch'}


def upload(d):
    import base64
    nome = os.path.basename(str(d.get('nome', 'arquivo'))).strip() or 'arquivo'
    nome = re.sub(r'[^\w.\- ()]', '_', nome)[:120]
    dados = base64.b64decode(d.get('b64', ''), validate=False)
    if not dados:
        return {'ok': False, 'msg': 'arquivo vazio'}
    os.makedirs(RECEBIDOS, exist_ok=True)
    dest = os.path.join(RECEBIDOS, nome)
    base, ext = os.path.splitext(nome)
    i = 1
    while os.path.exists(dest):
        dest = os.path.join(RECEBIDOS, base + ' (' + str(i) + ')' + ext)
        i += 1
    with open(dest, 'wb') as f:
        f.write(dados)
    try:
        os.startfile(RECEBIDOS)
    except Exception:
        pass
    log('recebido: ' + dest + ' (' + str(len(dados)) + ' bytes)')
    return {'ok': True, 'msg': 'salvo em ' + dest}


def clip_set(d):
    t = str(d.get('t', ''))[:200000]
    fd, path = tempfile.mkstemp(suffix='.txt', dir=BASE)
    os.close(fd)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(t)
        out, rc = run_ps("Get-Content -Raw -Encoding UTF8 -LiteralPath '" + path + "' | Set-Clipboard", 20)
        return {'ok': rc == 0, 'msg': 'copiado na tela touch' if rc == 0 else 'falhou: ' + out.strip()[:150]}
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def clip_get():
    out, rc = run_ps("Get-Clipboard -Raw", 20)
    return {'ok': rc == 0, 't': out.rstrip('\r\n')[:200000]}


KIOSK_CFG = os.path.join(BASE, 'kiosk.json')
_proj = {'t': 0, 'v': []}


def kiosk_cfg():
    try:
        with open(KIOSK_CFG, encoding='utf-8') as f:
            c = json.load(f)
    except Exception:
        c = {}
    return {'cidade': str(c.get('cidade', ''))[:80], 'lat': c.get('lat'), 'lon': c.get('lon'),
            'projetos': c.get('projetos') if isinstance(c.get('projetos'), list) else []}


def kiosk_salvar(d):
    proj = []
    for p in (d.get('projetos') or [])[:20]:
        nome = str(p.get('nome', '')).strip()[:40]
        url = str(p.get('url', '')).strip()
        if nome and re.match(r'^https?://[^\s]+$', url) and len(url) <= 500:
            proj.append({'nome': nome, 'url': url})
    lat, lon = d.get('lat'), d.get('lon')
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except Exception:
        lat = lon = None
    cfg = {'cidade': str(d.get('cidade', ''))[:80], 'lat': lat, 'lon': lon, 'projetos': proj}
    with open(KIOSK_CFG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=True)
    _proj['t'] = 0
    return {'ok': True, 'msg': 'kiosk salvo (' + str(len(proj)) + ' projetos)'}


def projetos():
    if time.time() - _proj['t'] < 15:
        return _proj['v']
    import urllib.request, urllib.error
    itens = kiosk_cfg()['projetos']
    res = [None] * len(itens)

    def testa(i, p):
        t0 = time.time()
        try:
            rq = urllib.request.Request(p['url'], headers={'User-Agent': 'painel-tela-touch'})
            with urllib.request.urlopen(rq, timeout=5) as r:
                ok = r.status < 500
        except urllib.error.HTTPError as e:
            ok = e.code < 500
        except Exception:
            ok = False
        res[i] = {'nome': p['nome'], 'url': p['url'], 'ok': ok, 'ms': int((time.time() - t0) * 1000)}
    ths = [threading.Thread(target=testa, args=(i, p)) for i, p in enumerate(itens)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(8)
    _proj['v'] = [r for r in res if r]
    _proj['t'] = time.time()
    return _proj['v']


def trocar_token(d):
    t = str(d.get('t', ''))
    if not re.match(r'^[A-Za-z0-9]{4,32}$', t):
        return {'ok': False, 'msg': 'token precisa ter 4 a 32 letras/numeros'}
    with open(TOKEN_FILE, 'w') as f:
        f.write(t)
    log('token trocado, reiniciando')
    ok, erro = relancar()
    return {'ok': ok, 'msg': 'token trocado, reiniciando o painel...' if ok else 'token gravado, mas nao reiniciou: ' + erro}


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
            elif rota == '/kiosk':
                self._send(200, KIOSK_HTML, 'text/html')
            elif rota == '/api/kioskcfg':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, kiosk_cfg())
            elif rota == '/api/projetos':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, projetos())
            elif rota == '/tela':
                self._send(200, TELA_HTML, 'text/html')
            elif rota == '/api/texto':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, {'t': MSG['t']})
            elif rota == '/api/clip':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, clip_get())
            elif rota == '/api/sistema':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, sistema())
            elif rota == '/api/shot':
                if not self._auth():
                    return self._send(401, {'erro': 'token errado'})
                self._send(200, shot(), 'image/jpeg')
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
            elif rota == '/api/kioskcfg':
                self._send(200, kiosk_salvar(d))
            elif rota == '/api/url':
                self._send(200, abrir_url(d))
            elif rota == '/api/upload':
                self._send(200, upload(d))
            elif rota == '/api/clipset':
                self._send(200, clip_set(d))
            elif rota == '/api/tecla':
                self._send(200, tecla(d))
            elif rota == '/api/brilho':
                self._send(200, brilho(d))
            elif rota == '/api/token':
                self._send(200, trocar_token(d))
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
