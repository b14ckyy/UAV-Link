#!/usr/bin/env python3
"""UAV-Link web UI: video pipeline and WWAN configuration (port 8080)."""
import glob
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time

from flask import (Flask, jsonify, redirect, render_template_string, request,
                   send_file)

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
PREVIEW_PATH = '/tmp/uav-preview.jpg'

DEFAULTS = {
    'device': 'auto', 'width': 720, 'height': 576, 'framerate': 50,
    'bitrate_kbps': 2000, 'bitrate_mode': 'vbr', 'codec': 'h264',
    'port': 8554, 'mount': '/cam',
}
MSP_DEFAULTS = {
    'link': 'off', 'uart_device': '/dev/serial0', 'baud': 115200,
    'udp_port': 5760, 'protocol': 'msp', 'inject_link_stats': True,
    'poll_status': True, 'arm_wifi_off': False, 'arm_wifi_delay': 30,
}
MSP_BAUDS = [115200, 230400, 460800, 921600]
OLED_DEFAULTS = {
    'enabled': True, 'controller': 'ssd1306', 'address': '0x3C',
    'width': 128, 'height': 64,
}

app = Flask(__name__)
preview_lock = threading.Lock()
preview_ts = 0.0

# --- Auth: IP-basierte Session (keine Cookies), Default-Passwort, 10-min-Timeout ---
AUTH_PATH = os.path.join(BASE, 'webui-auth.json')
DEFAULT_PW = 'uavlink2026'
SESSION_TIMEOUT = 600          # 10 min ohne Traffic -> Passwort wieder scharf
AUTHED = {}                    # ip -> letzter Traffic (monotonic)


def _hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, 100_000).hex()


def load_auth():
    try:
        with open(AUTH_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def set_password(pw):
    salt = os.urandom(16)
    tmp = AUTH_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'salt': salt.hex(), 'hash': _hash_pw(pw, salt)}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, AUTH_PATH)   # atomar -> nie eine 0-Byte-Datei


def verify_password(pw):
    a = load_auth()
    try:
        return bool(a) and _hash_pw(pw, bytes.fromhex(a['salt'])) == a['hash']
    except (KeyError, ValueError):
        return False


def is_default_password():
    return verify_password(DEFAULT_PW)


def ensure_auth():
    """Legt das Default-Passwort an, wenn die Datei fehlt/korrupt ist.
    -> Reset = Datei loeschen (self-healing zu Default)."""
    if load_auth() is None:
        set_password(DEFAULT_PW)


ensure_auth()                  # Erststart


def client_ip():
    return request.remote_addr or ''


def is_authed():
    ip = client_ip()
    ts = AUTHED.get(ip)
    if ts is not None and time.monotonic() - ts < SESSION_TIMEOUT:
        AUTHED[ip] = time.monotonic()      # Traffic haelt die Session offen
        return True
    return False


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


_throttled = {'ts': 0.0, 'val': 0}


def throttled_flags():
    """Unterspannungs-/Throttle-Bits, gecacht.

    `vcgencmd get_throttled` braucht gemessen ~9 ms und laeuft ueber die
    VideoCore-Mailbox -- also dieselbe Einheit, die den H.264-/JPEG-Encoder
    bedient. Die Stats-Seite pollt alle 2 s; ungecacht riss dieser Aufruf
    zuverlaessig je einen Frame heraus (ein Drop alle 2 s im Player, exakt im
    Takt). Der Throttle-Zustand aendert sich ohnehin selten."""
    now = time.monotonic()
    if now - _throttled['ts'] > 20:
        m = re.search(r'0x([0-9a-fA-F]+)', sh(['vcgencmd', 'get_throttled']))
        _throttled['val'] = int(m.group(1), 16) if m else 0
        _throttled['ts'] = now
    return _throttled['val']


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


def sysfs_name(dev):
    try:
        with open(f'/sys/class/video4linux/{os.path.basename(dev)}/name') as f:
            return f.read().strip()
    except OSError:
        return ''


def video_devices():
    devs = []
    for dev in sorted(glob.glob('/dev/video*'),
                      key=lambda d: int(re.sub(r'\D', '', d) or 999)):
        name = sysfs_name(dev)
        if 'bcm2835' in name or 'rpivid' in name:
            continue
        if 'MJPG' in sh(['v4l2-ctl', '-d', dev, '--list-formats']):
            devs.append({'path': dev, 'name': name})
    return devs


def device_formats(dev):
    """MJPG resolutions and framerates from v4l2 enumeration."""
    out = sh(['v4l2-ctl', '-d', dev, '--list-formats-ext'])
    formats, in_mjpg, size = [], False, None
    for line in out.splitlines():
        m = re.match(r"\s*\[\d+\]: '(\w+)'", line)
        if m:
            in_mjpg = m.group(1) == 'MJPG'
            continue
        if not in_mjpg:
            continue
        m = re.match(r'\s*Size: Discrete (\d+)x(\d+)', line)
        if m:
            size = {'width': int(m.group(1)), 'height': int(m.group(2)),
                    'fps': []}
            formats.append(size)
            continue
        m = re.match(r'\s*Interval: Discrete [\d.]+s \(([\d.]+) fps\)', line)
        if m and size is not None:
            fps = round(float(m.group(1)))
            if fps not in size['fps']:
                size['fps'].append(fps)
    return [f for f in formats if f['fps']]


def modem_info():
    out = sh(['mmcli', '-m', 'a', '--output-keyvalue'])
    kv = {}
    for line in out.splitlines():
        if ' : ' in line:
            k, v = line.split(' : ', 1)
            kv[k.strip()] = v.strip()
    sig = sh(['sudo', 'qmicli', '-d', '/dev/cdc-wdm0',
              '--nas-get-signal-info', '-p'])

    def grab(pat):
        m = re.search(pat + r": '([^']+)'", sig)
        return m.group(1) if m else '?'

    ip_out = sh(['ip', '-4', '-o', 'addr', 'show', 'wwan0'])
    ip_match = re.search(r'inet (\S+)', ip_out)
    return {
        'state': kv.get('modem.generic.state', 'no modem'),
        'operator': kv.get('modem.3gpp.operator-name', '?'),
        'tech': kv.get('modem.generic.access-technologies.value[1]', '?'),
        'quality': kv.get('modem.generic.signal-quality.value', '?'),
        'rsrp': grab('RSRP'), 'rsrq': grab('RSRQ'), 'snr': grab('SNR'),
        'apn': sh(['nmcli', '-g', 'gsm.apn', 'connection', 'show',
                   'uav-wwan']),
        'username': sh(['nmcli', '-g', 'gsm.username', 'connection', 'show',
                        'uav-wwan']),
        'wwan_ip': ip_match.group(1) if ip_match else '',
    }


def msp_config(cfg):
    m = dict(MSP_DEFAULTS)
    m.update(cfg.get('msp') or {})
    return m


def oled_config(cfg):
    o = dict(OLED_DEFAULTS)
    o.update(cfg.get('oled') or {})
    return o


def uav_version():
    info = {'channel': '', 'ref': '', 'commit': '', 'commit_date': '', 'updated': ''}
    try:
        with open(os.path.join(BASE, 'VERSION')) as f:
            raw = f.read().strip()
    except OSError:
        raw = ''
    try:
        info.update(json.loads(raw))
    except (ValueError, TypeError):
        info['ref'] = raw            # legacy plain-text VERSION ("main (2026-...Z)")

    def fmt(ts):                     # "2026-07-24T19:30:00Z" -> "2026-07-24 19:30Z"
        return (ts[:16].replace('T', ' ') + 'Z') if len(ts) >= 16 else ts
    head = info['channel'] or info['ref'] or 'unknown'
    if info['commit'] and info['commit'] != 'unknown':
        head += ' @ ' + info['commit']
    info['head'] = head
    info['commit_date_fmt'] = fmt(info['commit_date'])
    info['updated_fmt'] = fmt(info['updated'])
    return info


GITHUB_REPO = 'b14ckyy/UAV-Link'
_commits = {'ts': 0.0, 'data': [], 'err': ''}


def github_commits(limit=40):
    """Letzte Commits von main -- ueber den Atom-Feed, nicht ueber die API.
    Der Feed hat kein Rate-Limit (die API 60/h pro IP, was hinter CGNAT schnell
    aufgebraucht ist). Ergebnis wird kurz gecacht, damit nicht jeder Seitenaufruf
    eine Anfrage ausloest."""
    import urllib.request
    now = time.monotonic()
    if _commits['data'] and now - _commits['ts'] < 120:
        return _commits['data'], ''
    url = f'https://github.com/{GITHUB_REPO}/commits/main.atom'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'uav-link'})
        with urllib.request.urlopen(req, timeout=12) as r:
            xml = r.read().decode('utf-8', 'replace')
    except Exception as e:                      # offline, DNS, Timeout ...
        _commits['err'] = str(e)[:120]
        return _commits['data'], _commits['err']
    out = []
    for entry in re.findall(r'<entry>(.*?)</entry>', xml, re.S)[:limit]:
        m = re.search(r'Commit/([0-9a-f]{40})', entry)
        if not m:
            continue
        sha = m.group(1)
        t = re.search(r'<updated>([^<]+)</updated>', entry)
        ttl = re.search(r'<title>(.*?)</title>', entry, re.S)
        msg = re.sub(r'\s+', ' ', ttl.group(1)).strip() if ttl else ''
        for a, b in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"')):
            msg = msg.replace(a, b)
        when = (t.group(1)[:16].replace('T', ' ') + 'Z') if t else ''
        out.append({'sha': sha, 'short': sha[:7], 'when': when, 'msg': msg[:70]})
    if out:
        _commits.update(ts=now, data=out, err='')
    return out, ''


def wg_ip():
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)',
                  sh(['ip', '-4', '-o', 'addr', 'show', 'wgnet']))
    return m.group(1) if m else ''


def wg_status():
    """Tunnelstatus in drei Zustaenden.

    'up'   = Interface existiert, wg zeigt den Peer
    'down' = Config ist hinterlegt, Tunnel laeuft aber nicht (kein Link,
             Endpoint nicht erreichbar, Unit gescheitert)
    'none' = ueberhaupt keine Config hinterlegt

    'down' und 'none' sahen frueher identisch aus ("not configured") — das
    schickt den Operator zum Neu-Hochladen, obwohl die Config laengst da ist.
    Unterschieden wird ueber is-enabled: uav-wg-apply aktiviert die Unit beim
    Anwenden, eine hinterlegte Config ist also immer auch enabled. Das geht
    ohne sudo, /etc/wireguard selbst ist 0700 root und nicht statbar.
    """
    out = sh(['sudo', 'wg', 'show', 'wgnet'])
    if out:
        state = 'up'
    elif sh(['systemctl', 'is-enabled', 'wg-quick@wgnet']) == 'enabled':
        state = 'down'
    else:
        state = 'none'
    st = {'up': bool(out), 'state': state, 'endpoint': '', 'handshake': '',
          'transfer': '', 'address': wg_ip()}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('endpoint:'):
            st['endpoint'] = line.split(':', 1)[1].strip()
        elif line.startswith('latest handshake:'):
            st['handshake'] = line.split(':', 1)[1].strip()
        elif line.startswith('transfer:'):
            st['transfer'] = line.split(':', 1)[1].strip()
    return st


HOTSPOT = 'uav-hotspot'
RESERVED_CONNS = {HOTSPOT, 'uav-wwan'}


def wifi_status():
    """(ap_active, verbundene SSID/Profilname) fuer wlan0."""
    for line in sh(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION',
                    'device']).splitlines():
        parts = line.split(':')
        if parts[0] == 'wlan0' and len(parts) >= 3 and parts[1] == 'connected':
            if parts[2] == HOTSPOT:
                return True, ''
            return False, parts[2]
    return False, ''


def wifi_networks():
    nets = []
    for line in sh(['nmcli', '-t', '-f', 'NAME,TYPE',
                    'connection', 'show']).splitlines():
        name, _, typ = line.rpartition(':')
        if typ == '802-11-wireless' and name not in RESERVED_CONNS:
            nets.append(name)
    return nets


def hotspot_pw_default():
    psk = sh(['sudo', 'nmcli', '-s', '-g', '802-11-wireless-security.psk',
              'connection', 'show', HOTSPOT]).strip()
    return psk in ('', 'uavlink2026')


def net_counters():
    counters = {}
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if ':' not in line:
                    continue
                name, rest = line.split(':', 1)
                name = name.strip()
                if name in ('wwan0', 'wlan0'):
                    fields = rest.split()
                    counters[name] = {'rx': int(fields[0]),
                                      'tx': int(fields[8])}
    except OSError:
        pass
    return counters


def usb_backup_target():
    """First USB block device (never the SD) + its first filesystem partition.
    Detection only -- lsblk needs no root."""
    rows = []
    for line in sh(['lsblk', '-Pno',
                    'NAME,TYPE,TRAN,FSTYPE,SIZE,MODEL,PKNAME,MOUNTPOINT']).splitlines():
        d = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if d:
            rows.append(d)
    disk = next((r for r in rows
                 if r.get('TYPE') == 'disk' and r.get('TRAN') == 'usb'), None)
    if not disk:
        return None
    part = next((r for r in rows if r.get('TYPE') == 'part'
                 and r.get('PKNAME') == disk['NAME'] and r.get('FSTYPE')), None)
    return {
        'name': disk['NAME'], 'size': disk.get('SIZE', '?'),
        'model': (disk.get('MODEL') or '').strip(),
        'part': part['NAME'] if part else '',
        'fstype': part.get('FSTYPE', '') if part else '',
        'mountpoint': part.get('MOUNTPOINT', '') if part else '',
    }


TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UAV-Link</title>
<style>
  body { font-family: system-ui, sans-serif; background: #14181c; color: #d8dde2;
         max-width: 680px; margin: 2em auto; padding: 0 1em; }
  h1 { font-size: 1.4em; } h2 { font-size: 1.05em; margin-top: 1.6em;
       border-bottom: 1px solid #2a323a; padding-bottom: .3em; }
  .card { background: #1b2127; border: 1px solid #2a323a; border-radius: 8px;
          padding: 1em 1.2em; margin: 1em 0; }
  label { display: block; margin: .6em 0 .15em; font-size: .85em; color: #9aa5af; }
  input, select { width: 100%; box-sizing: border-box; padding: .45em;
          background: #12161a; color: #d8dde2; border: 1px solid #35404a;
          border-radius: 5px; }
  .row { display: flex; gap: .8em; } .row > div { flex: 1; }
  button { margin-top: 1em; padding: .55em 1.4em; background: #2f6fb3;
           color: #fff; border: 0; border-radius: 5px; cursor: pointer; }
  button:hover { background: #3a82cf; }
  button.secondary { background: #2a323a; }
  .kv { display: grid; grid-template-columns: auto 1fr; gap: .2em 1em;
        font-size: .9em; }
  .kv span:nth-child(odd) { color: #9aa5af; }
  .ok { color: #6fc276; } .bad { color: #e07a5f; }
  .url { font-family: monospace; background: #12161a; padding: .3em .5em;
         border-radius: 4px; display: inline-block; }
  .hint { font-size: .78em; color: #7c8791; margin-top: .2em; }
  .hint.bad { color: #e07a5f; }
  h3 { font-size: .92em; color: #c3ccd4; font-weight: 600; margin: .2em 0 .5em; }
  .sep { border: 0; border-top: 1px solid #2a323a; margin: 1.4em 0; }
  #preview-img { width: 100%; border-radius: 6px; margin-top: .8em;
                 display: none; background: #000; }
  .stat-grid { display: grid; grid-template-columns: repeat(3, 1fr);
               gap: .8em; text-align: center; }
  .stat { background: #12161a; border-radius: 6px; padding: .6em .3em; }
  .stat b { font-size: 1.15em; display: block; }
  .stat span { font-size: .72em; color: #7c8791; }
</style></head><body>
<h1>UAV-Link</h1>

<style>
  .warn { background: #4a1c1c; border: 1px solid #e07a5f; color: #ffd9cf;
          padding: .7em 1em; border-radius: 6px; margin: .7em 0; }
  .warn form { display: flex; gap: .5em; margin-top: .5em; }
  .warn input { margin: 0; }
</style>
{% if default_pw %}
<div class="warn">
  <b>&#9888; Default password active</b> — change it now (also change the Wi-Fi
  hotspot password below).
  <form method="post" action="/passwd" onsubmit="return pwMatch(this,'new_password')">
    <input name="new_password" type="password" placeholder="new UI password"
           minlength="4" required>
    <input name="confirm" type="password" placeholder="repeat password"
           minlength="4" required>
    <button type="submit">Change</button>
  </form>
</div>
{% endif %}
{% if ap_pw_warn %}
<div class="warn">
  <b>&#9888; Access-point mode with default Wi-Fi password</b> — change the hotspot
  password in the Wi-Fi section below.
</div>
{% endif %}

<div class="card">
  <div class="kv">
    <span>RTSP stream</span>
    <span class="url">rtsp://{{ host }}:{{ cfg.port }}{{ cfg.mount }}</span>
    <span>Video service</span>
    <span class="{{ 'ok' if rtsp_active else 'bad' }}">
      {{ 'running' if rtsp_active else 'stopped' }}</span>
  </div>
  <button type="button" class="secondary" id="preview-btn"
          onclick="togglePreview()">Start preview</button>
  <img id="preview-img" alt="stream preview">
  <div class="hint" id="preview-hint"></div>
</div>

<h2>System</h2>
<div class="card">
  <div class="stat-grid">
    <div class="stat"><b id="st-cpu">–</b><span>CPU load</span></div>
    <div class="stat"><b id="st-temp">–</b><span>SoC temp</span></div>
    <div class="stat"><b id="st-ram">–</b><span>RAM used</span></div>
    <div class="stat"><b id="st-wwan">–</b><span>4G Rx / Tx</span></div>
    <div class="stat"><b id="st-wlan">–</b><span>WiFi Rx / Tx</span></div>
    <div class="stat"><b id="st-sig">{{ modem.quality }} %</b>
      <span>signal</span></div>
    <div class="stat"><b id="st-pwr">–</b><span>supply voltage</span></div>
  </div>
</div>

<h2>Video</h2>
<div class="card"><form method="post" action="/save">
  <label>Device</label>
  <select name="device" id="device" onchange="loadFormats()">
    {% for d in devices %}
    <option value="{{ d.path }}" {{ 'selected' if cfg.device == d.path }}>
      {{ d.path }} — {{ d.name }}</option>
    {% endfor %}
    <option value="auto" {{ 'selected' if cfg.device == 'auto' }}>
      auto (first MJPG capture device)</option>
  </select>
  <div class="row">
    <div><label>Resolution</label>
      <select name="resolution" id="resolution"
              onchange="updateFps()"></select></div>
    <div><label>Frame rate</label>
      <select name="framerate" id="framerate" onchange="updateCodec()"></select></div>
  </div>
  <div class="row">
    <div><label>Codec</label>
      <select name="codec" id="codec" onchange="updateCodec()">
        <option value="h264" {{ 'selected' if cfg.codec not in ('mjpeg', 'mjpeg-src') }}>H.264 (recommended)</option>
        <option value="mjpeg" {{ 'selected' if cfg.codec == 'mjpeg' }}>MJPEG — encoded (~10 Mbit)</option>
        <option value="mjpeg-src" {{ 'selected' if cfg.codec == 'mjpeg-src' }}>MJPEG — source quality (LAN)</option>
      </select></div>
    <div id="bitrate-box"><label>Bitrate</label>
      <select name="bitrate_kbps">
        {% for kbps in bitrates %}
        <option value="{{ kbps }}" {{ 'selected' if cfg.bitrate_kbps == kbps }}>
          {{ '%.1f'|format(kbps / 1000) }} Mbit/s</option>
        {% endfor %}
      </select></div>
    <div id="ratectl-box"><label>Rate control</label>
      <select name="bitrate_mode">
        <option value="vbr" {{ 'selected' if cfg.bitrate_mode == 'vbr' }}>VBR (recommended)</option>
        <option value="cbr" {{ 'selected' if cfg.bitrate_mode == 'cbr' }}>CBR</option>
      </select></div>
  </div>
  <div class="hint" id="codec-hint"></div>
  <div class="row">
    <div><label>RTSP port</label>
      <input name="port" type="number" value="{{ cfg.port }}"></div>
    <div><label>Mount path</label>
      <input name="mount" value="{{ cfg.mount }}"></div>
  </div>
  <button type="submit">Save &amp; restart pipeline</button>
</form></div>

<h2>Network</h2>
<div class="card">
  <h3>Cellular (WWAN)</h3>
  <div class="kv">
    <span>Status</span><span>{{ modem.state }}</span>
    <span>Network</span><span>{{ modem.operator }} ({{ modem.tech }})</span>
    <span>Signal</span><span>{{ modem.quality }} % — RSRP {{ modem.rsrp }},
      RSRQ {{ modem.rsrq }}, SNR {{ modem.snr }}</span>
    <span>IP (wwan0)</span><span>{{ modem.wwan_ip or 'not connected' }}</span>
  </div>
  <form method="post" action="/apn">
    <label>APN (empty = modem default bearer)</label>
    <input name="apn" value="{{ modem.apn }}"
           placeholder="empty = use modem default">
    <div class="row">
      <div><label>Username (optional)</label>
        <input name="username" value="{{ modem.username }}"></div>
      <div><label>Password (optional)</label>
        <input name="password" type="password" placeholder="unchanged"></div>
    </div>
    <div class="row">
      <div><label>SIM PIN (empty = unchanged)</label>
        <input name="pin" type="password" maxlength="8"
               placeholder="only for locked SIMs"></div>
      <div><label>&nbsp;</label>
        <label style="display:flex;align-items:center;gap:.5em;margin:0">
          <input type="checkbox" name="clear_pin" value="1"
                 style="width:auto"> clear stored PIN</label></div>
    </div>
    <div class="hint">Empty APN = modem default bearer (try first). SIM PIN only for locked cards.</div>
    <button type="submit">Save APN &amp; reconnect</button>
  </form>

  <hr class="sep">

  <h3>Wi-Fi</h3>
  <div class="kv">
    <span>Radio</span>
    <span class="{{ 'ok' if wifi_on else 'bad' }}">
      {{ 'enabled' if wifi_on else 'disabled (until reboot)' }}</span>
    <span>Mode</span>
    <span>{% if ap_active %}<b class="ok">access point</b> — SSID "UAV-Link",
      page at http://10.42.0.1:8080{% elif wifi_ssid %}client — {{ wifi_ssid }}
      {% else %}not connected{% endif %}</span>
  </div>
  <div class="row">
    <div>
      <form method="post" action="/wifi" id="wifi-form" onsubmit="return confirmWifi();">
        <input type="hidden" name="action" value="{{ 'off' if wifi_on else 'on' }}">
        <button type="submit" class="{{ 'secondary' if wifi_on else '' }}">
          {{ 'Disable Wi-Fi (runtime only)' if wifi_on else 'Enable Wi-Fi' }}</button>
      </form>
    </div>
    <div>
      <form method="post" action="/wifi">
        <input type="hidden" name="action" value="{{ 'ap_off' if ap_active else 'ap_on' }}">
        <button type="submit" class="secondary">
          {{ 'Stop access point' if ap_active else 'Start access point' }}</button>
      </form>
    </div>
  </div>
  <div class="hint">Forced ON at boot. No known network within 60 s &rarr; access point
    <b>UAV-Link</b> (pw <b>uavlink2026</b>, http://10.42.0.1:8080); or hold GPIO21/pin40
    3 s. Disabling drops LAN &mdash; continue over the VPN{% if vpn_ip %} at
    <b>http://{{ vpn_ip }}:8080</b>{% endif %}.</div>
  <form method="post" action="/hotspot_pw" onsubmit="return pwMatch(this,'psk')">
    <label>Access-point (hotspot) password</label>
    <div class="row">
      <div><input name="psk" type="password" minlength="8" maxlength="63"
             placeholder="new hotspot password (min 8)"></div>
      <div><input name="confirm" type="password" minlength="8" maxlength="63"
             placeholder="repeat"></div>
    </div>
    <button type="submit" class="secondary">Change hotspot password</button>
  </form>

  <hr class="sep">

  <h3>Known networks</h3>
  {% if wifi_nets %}
  <table style="width:100%;border-collapse:collapse">
    {% for n in wifi_nets %}
    <tr style="border-bottom:1px solid #2a323a">
      <td style="padding:.35em .4em">{{ n }}{% if n == wifi_ssid %}
        <span class="ok">(connected)</span>{% endif %}</td>
      <td style="padding:.35em .4em;text-align:right">
        <form method="post" action="/wifi_net" style="display:inline"
              onsubmit="return confirm('Remove network {{ n }}?');">
          <input type="hidden" name="action" value="delete">
          <input type="hidden" name="name" value="{{ n }}">
          <button type="submit" class="secondary"
                  style="margin:0;padding:.25em .7em">Remove</button>
        </form></td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="hint">No saved networks.</div>
  {% endif %}
  <form method="post" action="/wifi_net">
    <input type="hidden" name="action" value="add">
    <div class="row">
      <div><label>SSID</label><input name="ssid" maxlength="32"></div>
      <div><label>Password (WPA2, empty = open)</label>
        <input name="password" type="password" maxlength="63"></div>
    </div>
    <button type="submit">Add network</button>
  </form>
</div>

<h2>VPN (WireGuard)</h2>
<div class="card">
  <div class="kv">
    <span>Tunnel</span>
    <span class="{{ 'ok' if wg.handshake and 'ago' in wg.handshake else 'bad' }}">
      {% if wg.state == 'up' %}up
      {% elif wg.state == 'down' %}configured, down
      {% else %}not configured{% endif %}</span>
    <span>VPN IP</span><span>{{ wg.address or '–' }}</span>
    <span>Endpoint</span><span>{{ wg.endpoint or '–' }}</span>
    <span>Last handshake</span><span>{{ wg.handshake or 'never' }}</span>
    <span>Transfer</span><span>{{ wg.transfer or '–' }}</span>
  </div>
  <form method="post" action="/wg"
        onsubmit="return confirm('Replace the current tunnel config? The old one is backed up.');">
    <label>Upload a .conf file, or paste it below</label>
    <input type="file" accept=".conf,text/plain" onchange="loadWgFile(this)"
           style="margin-bottom:.4em">
    <label>Client config (paste the .conf from your WireGuard server)</label>
    <textarea name="config" rows="9" spellcheck="false"
      style="width:100%;box-sizing:border-box;padding:.45em;background:#12161a;
             color:#d8dde2;border:1px solid #35404a;border-radius:5px;
             font-family:monospace;font-size:.82em;resize:vertical"
      placeholder="[Interface]&#10;PrivateKey = ...&#10;Address = ...&#10;&#10;[Peer]&#10;PublicKey = ...&#10;Endpoint = host:51820&#10;AllowedIPs = ..."></textarea>
    <button type="submit">Apply config &amp; restart tunnel</button>
  </form>
  <div class="hint">Replaces the current tunnel (previous config is backed up on
    the Pi). The peer endpoint is automatically pinned to the LTE interface, so
    the tunnel always runs over cellular even when Wi-Fi is up. Paste the config
    your WireGuard server generated for this device.</div>
</div>

<h2>Flight Controller (MSP)</h2>
<div class="card">
  <div class="kv">
    <span>Bridge service</span>
    <span class="{{ 'ok' if msp_active else 'bad' }}">
      {{ 'running' if msp_active else 'stopped' }}</span>
    <span>GCS endpoint</span>
    <span class="url">udp://{{ vpn_ip or host }}:{{ msp.udp_port }}</span>
  </div>
  <form method="post" action="/msp">
    <div class="row">
      <div><label>FC link</label>
        <select name="link">
          <option value="off" {{ 'selected' if msp.link == 'off' }}>disabled</option>
          <option value="uart" {{ 'selected' if msp.link == 'uart' }}>UART (GPIO 14/15)</option>
          <option value="usb" {{ 'selected' if msp.link == 'usb' }}>USB VCP (auto /dev/ttyACM*)</option>
        </select></div>
      <div><label>Baud rate (UART)</label>
        <select name="baud">
          {% for b in msp_bauds %}
          <option value="{{ b }}" {{ 'selected' if msp.baud == b }}>{{ b }}</option>
          {% endfor %}
        </select></div>
    </div>
    <div class="row">
      <div><label>Protocol</label>
        <select name="protocol">
          <option value="msp" {{ 'selected' if msp.protocol == 'msp' }}>MSP (INAV)</option>
          <option value="mavlink" {{ 'selected' if msp.protocol == 'mavlink' }}>MAVLink (ArduPilot/PX4)</option>
        </select></div>
      <div><label>UDP port</label>
        <input name="udp_port" type="number" value="{{ msp.udp_port }}"></div>
    </div>
    <div class="row">
      <div><label>LTE telemetry injection</label>
        <select name="inject">
          <option value="on" {{ 'selected' if msp.inject_link_stats }}>on (RSSI/SNR → OSD/GCS)</option>
          <option value="off" {{ 'selected' if not msp.inject_link_stats }}>off</option>
        </select></div>
      <div><label>Disable Wi-Fi when armed</label>
        <select name="arm_wifi_off">
          <option value="off" {{ 'selected' if not msp.arm_wifi_off }}>off</option>
          <option value="on" {{ 'selected' if msp.arm_wifi_off }}>on (after {{ msp.arm_wifi_delay }} s, LTE stays)</option>
        </select></div>
    </div>
    <div class="hint">Configure the FC port for the selected protocol at the same baud
      (INAV: Ports → MSP; ArduPilot: SERIALn_PROTOCOL = MAVLink). Cellular link stats
      are injected as RC link telemetry (MSP: RC_LINK_STATS, MAVLink: RADIO_STATUS).
      "Disable Wi-Fi when armed" cuts Wi-Fi {{ msp.arm_wifi_delay }} s after arming
      (LTE/VPN stay up) — the bridge reads the arm state passively; it never polls the
      FC while a GCS is connected.</div>
    <button type="submit">Save &amp; restart bridge</button>
  </form>
</div>

<h2>Software Update</h2>
<div class="card">
  <div class="kv"><span>Installed</span><span>{{ version.head }}</span></div>
  {% if version.commit_date_fmt or version.updated_fmt %}
  <div class="hint" style="margin:-4px 0 8px">
    {%- if version.commit_date_fmt %}committed {{ version.commit_date_fmt }}{% endif %}
    {%- if version.commit_date_fmt and version.updated_fmt %} &middot; {% endif %}
    {%- if version.updated_fmt %}updated {{ version.updated_fmt }}{% endif %}
  </div>{% endif %}
  <form method="post" action="/update"
        onsubmit="return confirm('Update now? The web UI is unavailable for ~1-2 min.');">
    <div class="row">
      <div><label>Channel</label>
        <select name="channel" id="upd-channel" onchange="updChannel()">
          <option value="releases"{{ ' selected' if version.channel=='releases' else '' }}>Releases (stable)</option>
          <option value="beta"{{ ' selected' if version.channel=='beta' else '' }}>Beta (pre-releases)</option>
          <option value="development"{{ ' selected' if version.channel.startswith('development') else '' }}>Development (main)</option>
        </select></div>
      <div style="display:flex;align-items:flex-end">
        <button type="submit" style="margin:0">Update now</button></div>
    </div>
    <div id="upd-commit-box" style="display:none">
      <label>Commit (Development only — pick an older build to downgrade to)</label>
      <select name="commit" id="upd-commit">
        <option value="">latest on main (default)</option>
      </select>
      <div class="hint" id="upd-commit-hint">Loading commit list…</div>
    </div>
  </form>
  <div class="hint">Re-runs the installer from the selected channel (over LTE or Wi-Fi).
    Config and password are preserved. Log: <code>/var/log/uav-update.log</code>.</div>
</div>

<h2>System Backup</h2>
<div class="card">
  <div class="kv">
    <span>USB target</span>
    <span>{% if usb %}{{ usb.model or ('/dev/' + usb.name) }} &middot; {{ usb.size }}
      {% if usb.fstype %}&middot; {{ usb.fstype }}{% else %}<span class="bad">(no filesystem)</span>{% endif %}
      {% else %}<span class="bad">none detected</span>{% endif %}</span>
  </div>
  {% if usb and usb.fstype == 'vfat' %}
  <div class="hint bad">FAT32 target: images larger than 4 GiB will fail &mdash; use exFAT/ext4 for big cards.</div>
  {% endif %}
  <form method="post" action="/backup"
        onsubmit="return confirm('Write a full compressed image of the SD card to the USB stick?\\nThis reads the whole card and can take 10-30 min.');">
    <button type="submit"{% if not (usb and usb.fstype) %} disabled{% endif %}>Create backup to USB</button>
  </form>
  <div id="backup-status" class="hint" style="margin-top:.8em"></div>
  <div class="hint">Writes a compressed <code>.img.gz</code> of the whole SD card as a file
    onto the stick (its existing data is kept). Plug in a stick and reload if none is
    listed. Log: <code>/var/log/uav-backup.log</code>.</div>
</div>

<h2>Status Display (OLED)</h2>
<div class="card"><form method="post" action="/oled">
  <div class="row">
    <div><label>Display</label>
      <select name="enabled">
        <option value="on" {{ 'selected' if oled.enabled }}>enabled</option>
        <option value="off" {{ 'selected' if not oled.enabled }}>disabled</option>
      </select></div>
    <div><label>Controller</label>
      <select name="controller">
        <option value="ssd1306" {{ 'selected' if oled.controller == 'ssd1306' }}>SSD1306 (0.96")</option>
        <option value="sh1106" {{ 'selected' if oled.controller == 'sh1106' }}>SH1106 (1.3")</option>
      </select></div>
    <div><label>I2C address</label>
      <select name="address">
        <option value="0x3C" {{ 'selected' if oled.address == '0x3C' }}>0x3C</option>
        <option value="0x3D" {{ 'selected' if oled.address == '0x3D' }}>0x3D</option>
      </select></div>
  </div>
  <div class="hint">128&times;64 I2C OLED on GPIO2/3 (pins 3/5, GND pin 6). Pick SH1106
    for 1.3" panels — avoids the 2&#8209;pixel shift / scrambled rows. Needs I2C enabled
    (active after the next reboot on first setup).</div>
  <button type="submit">Save &amp; restart display</button>
</form></div>

<script>
const CUR = { res: "{{ cfg.width }}x{{ cfg.height }}",
              fps: "{{ cfg.framerate }}" };
let formats = [];

function loadFormats() {
  const dev = document.getElementById('device').value;
  fetch('/api/formats?device=' + encodeURIComponent(dev))
    .then(r => r.json())
    .then(data => {
      formats = data.formats || [];
      const rsel = document.getElementById('resolution');
      rsel.innerHTML = '';
      let found = false;
      for (const f of formats) {
        const val = f.width + 'x' + f.height;
        const opt = new Option(val, val, false, val === CUR.res);
        rsel.add(opt);
        if (val === CUR.res) found = true;
      }
      if (!found && CUR.res !== 'x') {
        rsel.add(new Option(CUR.res + ' (current)', CUR.res, false, true));
      }
      updateFps();
    })
    .catch(() => {});
}

function updateFps() {
  const rsel = document.getElementById('resolution');
  const fsel = document.getElementById('framerate');
  fsel.innerHTML = '';
  const f = formats.find(x => x.width + 'x' + x.height === rsel.value);
  const rates = f ? f.fps.slice().sort((a, b) => b - a) : [];
  let found = false;
  for (const r of rates) {
    fsel.add(new Option(r + ' fps', r, false, String(r) === CUR.fps));
    if (String(r) === CUR.fps) found = true;
  }
  if (!found && CUR.fps) {
    fsel.add(new Option(CUR.fps + ' fps (current)', CUR.fps, false, true));
  }
  updateCodec();   // erst hier stehen Aufloesung/fps fest (Formate kommen per fetch)
}

// Messwerte 26.07.: der HW-JPEG-Encoder haelt ~10.5 Mbit und ignoriert jede
// Qualitaetsvorgabe. Konstante Bitrate -> die Qualitaet haengt an bit/px.
const MJPEG_HW_MBIT = 10.5;
const MJPEG_NATIVE_KB = { "1280x720": 78.0, "720x480": 42.0, "720x576": 44.0 };
function updateCodec() {
  const codec = document.getElementById('codec').value;
  const mjpeg = codec === 'mjpeg' || codec === 'mjpeg-src';
  document.getElementById('bitrate-box').style.display = mjpeg ? 'none' : '';
  document.getElementById('ratectl-box').style.display = mjpeg ? 'none' : '';
  const hint = document.getElementById('codec-hint');
  if (!mjpeg) {
    hint.className = 'hint';
    hint.textContent = 'Note: CBR throttles the HW encoder to ~38 fps (measured) — testing only.';
    return;
  }
  const res = document.getElementById('resolution').value;
  const fps = parseInt(document.getElementById('framerate').value, 10) || 0;
  const p = res.split('x');
  const px = (parseInt(p[0], 10) || 0) * (parseInt(p[1], 10) || 0);
  if (!px || !fps) {          // Formate noch nicht geladen -> keine Fantasiezahl zeigen
    hint.className = 'hint';
    hint.textContent = 'Reading supported formats from the capture device…';
    return;
  }
  if (codec === 'mjpeg-src') {
    let kb = MJPEG_NATIVE_KB[res];
    if (kb === undefined) kb = px ? 78.0 * px / (1280 * 720) : 0;
    hint.className = 'hint';
    hint.innerHTML = 'Source JPEG passed through untouched — best quality, no CPU cost, ' +
      'but roughly <b>' + (kb * 1024 * 8 * fps / 1e6).toFixed(0) + ' Mbit/s</b> at ' +
      res + ' @' + fps + ' fps. Meant for LAN/Wi-Fi. Needs a source that outputs ' +
      'MJPEG (USB capture sticks do; CSI cameras do not).';
    return;
  }
  const bpp = px && fps ? MJPEG_HW_MBIT * 1e6 / fps / px : 0;
  let msg = 'Hardware JPEG encode, fixed at about <b>' + MJPEG_HW_MBIT.toFixed(1) +
    ' Mbit/s</b> — the encoder ignores any quality setting, so lower resolution or ' +
    'frame rate buys picture quality, not bandwidth. At ' + res + ' @' + fps +
    ' fps that budget yields <b>' + bpp.toFixed(2) + ' bit/pixel</b>.';
  if (bpp && bpp < 0.25) {
    hint.className = 'hint bad';
    msg += ' <b>Too little — expect blocky artefacts.</b> Halve the frame rate or ' +
      'pick a lower resolution, or switch to "source quality" if bandwidth allows.';
  } else {
    hint.className = 'hint';
  }
  hint.innerHTML = msg;
}

let commitsLoaded = false;
function updChannel() {
  const dev = document.getElementById('upd-channel').value === 'development';
  document.getElementById('upd-commit-box').style.display = dev ? '' : 'none';
  if (!dev || commitsLoaded) return;
  commitsLoaded = true;
  const sel = document.getElementById('upd-commit');
  const hint = document.getElementById('upd-commit-hint');
  fetch('/api/commits').then(r => r.json()).then(d => {
    if (d.error) { hint.textContent = 'Commit list unavailable: ' + d.error; return; }
    for (const c of d.commits) {
      sel.add(new Option(c.when + '  ' + c.short + '  ' + c.msg, c.sha));
    }
    hint.textContent = d.commits.length + ' commits listed, newest first. ' +
      'Leave on "latest" for a normal update.';
  }).catch(e => { hint.textContent = 'Commit list unavailable.'; commitsLoaded = false; });
}

function pwMatch(f, name) {
  if (f[name].value !== f.confirm.value) {
    alert('Passwords do not match.');
    return false;
  }
  return true;
}

function loadWgFile(input) {
  const f = input.files && input.files[0];
  if (!f) return;
  const r = new FileReader();
  r.onload = e => {
    const ta = document.querySelector('textarea[name=config]');
    if (ta) ta.value = e.target.result;
  };
  r.readAsText(f);
}

function confirmWifi() {
  const act = document.querySelector('#wifi-form input[name=action]').value;
  if (act === 'off') {
    return confirm('Disable Wi-Fi until the next reboot?\\n' +
                   'This page stays reachable over the VPN{% if vpn_ip %} at ' +
                   'http://{{ vpn_ip }}:8080{% endif %} (WireGuard/LTE).');
  }
  return true;
}

let prev = null;
function fmtRate(bps) {
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + ' Mbit/s';
  if (bps >= 1e3) return (bps / 1e3).toFixed(0) + ' kbit/s';
  return bps.toFixed(0) + ' bit/s';
}
function pollStats() {
  fetch('/api/stats').then(r => r.json()).then(s => {
    document.getElementById('st-temp').textContent =
      s.temp_c.toFixed(1) + ' °C';
    document.getElementById('st-ram').textContent =
      Math.round((1 - s.mem_avail / s.mem_total) * 100) + ' %';
    const pwr = document.getElementById('st-pwr');
    if (s.throttled & 0x1) {
      pwr.textContent = 'LOW NOW!'; pwr.className = 'bad';
    } else if (s.throttled & 0x10000) {
      pwr.textContent = 'was low'; pwr.className = 'bad';
    } else {
      pwr.textContent = 'OK'; pwr.className = 'ok';
    }
    if (prev) {
      const dt = (s.ts - prev.ts) || 1;
      const busy = (s.cpu.total - prev.cpu.total) -
                   (s.cpu.idle - prev.cpu.idle);
      const pct = 100 * busy / ((s.cpu.total - prev.cpu.total) || 1);
      document.getElementById('st-cpu').textContent =
        Math.max(0, pct).toFixed(0) + ' %';
      for (const [iface, el] of [['wwan0', 'st-wwan'], ['wlan0', 'st-wlan']]) {
        if (s.net[iface] && prev.net[iface]) {
          const rx = 8 * (s.net[iface].rx - prev.net[iface].rx) / dt;
          const tx = 8 * (s.net[iface].tx - prev.net[iface].tx) / dt;
          document.getElementById(el).textContent =
            fmtRate(rx) + ' / ' + fmtRate(tx);
        } else {
          document.getElementById(el).textContent = 'down';
        }
      }
    }
    prev = s;
  }).catch(() => {});
}

let previewTimer = null;
function togglePreview() {
  const img = document.getElementById('preview-img');
  const btn = document.getElementById('preview-btn');
  const hint = document.getElementById('preview-hint');
  if (previewTimer) {
    clearInterval(previewTimer); previewTimer = null;
    img.style.display = 'none'; btn.textContent = 'Start preview';
    hint.textContent = '';
    return;
  }
  btn.textContent = 'Stop preview';
  hint.textContent = 'Grabbing frames from the RTSP stream (approx. one per 3 s)...';
  const refresh = () => {
    const probe = new Image();
    probe.onload = () => { img.src = probe.src; img.style.display = 'block';
                           hint.textContent = ''; };
    probe.onerror = () => { hint.textContent =
      'No frame available (stream idle or no input signal).'; };
    probe.src = '/preview.jpg?ts=' + Date.now();
  };
  refresh();
  previewTimer = setInterval(refresh, 3000);
}

function fmtSize(b) {
  if (!b) return '0 B';
  const u = ['B', 'KiB', 'MiB', 'GiB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
}
function pollBackup() {
  const el = document.getElementById('backup-status');
  if (!el) return;
  fetch('/api/backup').then(r => r.json()).then(s => {
    const name = (s.dest || '').split('/').pop();
    if (s.state === 'starting') {
      el.innerHTML = 'Starting backup…';
      setTimeout(pollBackup, 1500);
    } else if (s.state === 'running') {
      const pct = (s.percent != null) ? s.percent + '% ' : '';
      const done = s.bytes ? '(' + fmtSize(s.bytes) + ' of ' + fmtSize(s.total) + ' read)' : '';
      el.innerHTML = 'Backing up… ' + pct + '<span class="hint">' + done + '</span>';
      setTimeout(pollBackup, 3000);
    } else if (s.state === 'done') {
      el.innerHTML = '<span class="ok">Done:</span> ' + name +
                     ' (' + fmtSize(s.size) + ') — safe to remove the stick.';
    } else if (s.state === 'error') {
      el.innerHTML = '<span class="bad">Backup failed:</span> ' + (s.error || 'unknown');
    } else {
      el.textContent = '';
    }
  }).catch(() => {});
}

loadFormats();
updateCodec();
updChannel();
pollStats();
setInterval(pollStats, 2000);
pollBackup();
</script>
</body></html>"""


LOGIN_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UAV-Link — Login</title>
<style>
  body { font-family: system-ui, sans-serif; background: #14181c; color: #d8dde2;
         max-width: 340px; margin: 6em auto; padding: 0 1em; text-align: center; }
  h1 { font-size: 1.3em; } input { width: 100%; box-sizing: border-box; padding: .6em;
       margin: .6em 0; background: #12161a; color: #d8dde2; border: 1px solid #35404a;
       border-radius: 5px; }
  button { padding: .6em 1.6em; background: #2f6fb3; color: #fff; border: 0;
           border-radius: 5px; cursor: pointer; }
  .err { color: #e07a5f; font-size: .9em; }
</style></head><body>
<h1>UAV-Link</h1>
<form method="post" action="/login">
  <input name="password" type="password" placeholder="password" autofocus>
  <button type="submit">Log in</button>
  {% if err %}<div class="err">Wrong password.</div>{% endif %}
</form>
</body></html>"""


UPDATING_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="100; url=/">
<title>UAV-Link — Updating</title>
<style>body{font-family:system-ui,sans-serif;background:#14181c;color:#d8dde2;
  max-width:420px;margin:6em auto;padding:0 1em;text-align:center}
  b{color:#6fc276}</style></head><body>
<h1>Updating…</h1>
<p>Installing from the <b>{{ channel }}</b> channel. The web service restarts during
the update, so this page reloads automatically in ~100&nbsp;s. You may need to log in
again afterwards. If it doesn't return, wait a moment and reload.</p>
</body></html>"""


@app.before_request
def _require_auth():
    ensure_auth()              # geloeschte Auth-Datei -> Default (Reset self-heal)
    if request.path == '/login' or request.path.startswith('/static'):
        return None
    if not is_authed():
        return render_template_string(LOGIN_TEMPLATE, err=False)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if verify_password(request.form.get('password', '')):
            AUTHED[client_ip()] = time.monotonic()
            return redirect('/')
        return render_template_string(LOGIN_TEMPLATE, err=True)
    if is_authed():
        return redirect('/')
    return render_template_string(LOGIN_TEMPLATE, err=False)


@app.route('/passwd', methods=['POST'])
def passwd():
    new = request.form.get('new_password', '')
    confirm = request.form.get('confirm', '')
    if is_authed() and 4 <= len(new) <= 64 and new == confirm:
        set_password(new)
    return redirect('/')


@app.route('/hotspot_pw', methods=['POST'])
def hotspot_pw():
    psk = request.form.get('psk', '')
    confirm = request.form.get('confirm', '')
    if is_authed() and 8 <= len(psk) <= 63 and psk == confirm:
        sh(['sudo', 'nmcli', 'connection', 'modify', HOTSPOT,
            'wifi-sec.psk', psk])
    return redirect('/')


@app.route('/')
def index():
    cfg = load_config()
    return render_template_string(
        TEMPLATE, cfg=cfg, devices=video_devices(),
        modem=modem_info(), host=request.host.split(':')[0],
        bitrates=list(range(1000, 5001, 500)),
        msp=msp_config(cfg), msp_bauds=MSP_BAUDS, vpn_ip=wg_ip(),
        oled=oled_config(cfg), version=uav_version(),
        usb=usb_backup_target(),
        default_pw=is_default_password(),
        ap_pw_warn=(wifi_status()[0] and hotspot_pw_default()),
        wifi_on=sh(['nmcli', 'radio', 'wifi']) == 'enabled',
        ap_active=wifi_status()[0], wifi_ssid=wifi_status()[1],
        wifi_nets=wifi_networks(), wg=wg_status(),
        rtsp_active=sh(['systemctl', 'is-active', 'uav-rtsp']) == 'active',
        msp_active=sh(['systemctl', 'is-active', 'uav-msp']) == 'active')


@app.route('/api/formats')
def api_formats():
    dev = request.args.get('device', 'auto')
    if dev == 'auto':
        devs = video_devices()
        dev = devs[0]['path'] if devs else ''
    if not re.fullmatch(r'/dev/video\d+', dev or ''):
        return jsonify({'formats': []})
    return jsonify({'formats': device_formats(dev)})


@app.route('/api/stats')
def api_stats():
    temp = 0.0
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            temp = int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        pass
    mem_total = mem_avail = 0
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    mem_avail = int(line.split()[1])
    except OSError:
        pass
    cpu = {'total': 0, 'idle': 0}
    try:
        with open('/proc/stat') as f:
            fields = [int(x) for x in f.readline().split()[1:]]
        cpu = {'total': sum(fields), 'idle': fields[3] + fields[4]}
    except (OSError, ValueError, IndexError):
        pass
    throttled = throttled_flags()
    return jsonify({'ts': time.time(), 'temp_c': temp, 'cpu': cpu,
                    'mem_total': mem_total or 1, 'mem_avail': mem_avail,
                    'net': net_counters(), 'throttled': throttled})


@app.route('/preview.jpg')
def preview():
    global preview_ts
    cfg = load_config()
    with preview_lock:
        if time.time() - preview_ts > 2.0:
            try:
                os.unlink(PREVIEW_PATH)
            except OSError:
                pass
            url = f"rtsp://127.0.0.1:{cfg['port']}{cfg['mount']}"
            subprocess.run(
                ['gst-launch-1.0', '-q', 'rtspsrc', f'location={url}',
                 'latency=0', 'protocols=udp', '!', 'rtph264depay', '!',
                 'h264parse', '!', 'avdec_h264', '!', 'videoconvert', '!',
                 'jpegenc', 'snapshot=true', '!', 'filesink',
                 f'location={PREVIEW_PATH}'],
                capture_output=True, timeout=10, check=False)
            preview_ts = time.time()
    if os.path.exists(PREVIEW_PATH) and os.path.getsize(PREVIEW_PATH) > 0:
        resp = send_file(PREVIEW_PATH, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    return ('no frame', 503)


@app.route('/save', methods=['POST'])
def save():
    cfg = load_config()
    device = request.form.get('device', 'auto')
    if device == 'auto' or re.fullmatch(r'/dev/video\d+', device):
        cfg['device'] = device
    res = request.form.get('resolution', '')
    m = re.fullmatch(r'(\d{2,4})x(\d{2,4})', res)
    if m:
        cfg['width'], cfg['height'] = int(m.group(1)), int(m.group(2))
    mount = request.form.get('mount', '').strip().lstrip('/')
    if re.fullmatch(r'[A-Za-z0-9_\-]+', mount):
        cfg['mount'] = '/' + mount
    cfg['bitrate_mode'] = ('cbr' if request.form.get('bitrate_mode') == 'cbr'
                           else 'vbr')
    codec = request.form.get('codec')
    cfg['codec'] = codec if codec in ('mjpeg', 'mjpeg-src') else 'h264'
    for key in ('framerate', 'bitrate_kbps', 'port'):
        try:
            val = int(request.form.get(key, DEFAULTS[key]))
        except ValueError:
            val = DEFAULTS[key]
        cfg[key] = val if 0 < val <= 65535 else DEFAULTS[key]
    save_config(cfg)
    sh(['sudo', 'systemctl', 'restart', 'uav-rtsp.service'], timeout=30)
    return redirect('/')


@app.route('/wifi', methods=['POST'])
def wifi():
    action = request.form.get('action', '')
    if action in ('on', 'off'):
        sh(['sudo', 'nmcli', 'radio', 'wifi', action], timeout=20)
    elif action == 'ap_on':
        sh(['sudo', 'nmcli', 'radio', 'wifi', 'on'], timeout=20)
        sh(['sudo', 'nmcli', 'connection', 'up', HOTSPOT], timeout=60)
    elif action == 'ap_off':
        sh(['sudo', 'nmcli', 'connection', 'down', HOTSPOT], timeout=60)
    return redirect('/')


@app.route('/wifi_net', methods=['POST'])
def wifi_net():
    action = request.form.get('action', '')
    if action == 'add':
        ssid = request.form.get('ssid', '').strip()
        password = request.form.get('password', '')
        if 0 < len(ssid) <= 32 and (not password or 8 <= len(password) <= 63):
            args = ['sudo', 'nmcli', 'connection', 'add', 'type', 'wifi',
                    'ifname', 'wlan0', 'con-name', ssid, 'ssid', ssid,
                    'connection.autoconnect', 'yes']
            if password:
                args += ['wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password]
            sh(args)
    elif action == 'delete':
        name = request.form.get('name', '')
        if name in wifi_networks():   # nur echte WLAN-Profile, nie uav-wwan/hotspot
            sh(['sudo', 'nmcli', 'connection', 'delete', name])
    return redirect('/')


@app.route('/wg', methods=['POST'])
def wg_apply():
    config = request.form.get('config', '')
    if ('[Interface]' in config and '[Peer]' in config
            and 'PrivateKey' in config and len(config) < 8192):
        fd, path = tempfile.mkstemp(prefix='uav-wg-', suffix='.conf')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(config)
            os.chmod(path, 0o600)
            sh(['sudo', os.path.join(BASE, 'uav-wg-apply'), path], timeout=45)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return redirect('/')


@app.route('/update', methods=['POST'])
def update():
    channel = request.form.get('channel', 'releases')
    if channel not in ('releases', 'beta', 'development'):
        return redirect('/')
    target, label = channel, channel
    commit = (request.form.get('commit') or '').strip().lower()
    if channel == 'development' and commit:
        # Nur echte Hex-Hashes durchlassen -- der Wert landet in einem systemd-
        # Unit-Namen und in einer URL, also strikt validieren statt vertrauen.
        if not re.fullmatch(r'[0-9a-f]{7,40}', commit):
            return redirect('/')
        target, label = commit, f'development @ {commit[:7]}'
    sh(['sudo', 'systemctl', 'start', '--no-block',
        f'uav-update@{target}.service'], timeout=15)
    return render_template_string(UPDATING_TEMPLATE, channel=label)


@app.route('/api/commits')
def api_commits():
    commits, err = github_commits()
    return jsonify({'commits': commits, 'error': err})


@app.route('/backup', methods=['POST'])
def backup():
    # only start if a USB stick with a filesystem is actually present
    usb = usb_backup_target()
    if usb and usb.get('fstype'):
        sh(['sudo', 'systemctl', 'start', '--no-block',
            'uav-backup.service'], timeout=15)
    return redirect('/')


@app.route('/api/backup')
def api_backup():
    try:
        with open('/run/uav-backup.status') as f:
            st = json.load(f)
    except (OSError, ValueError):
        return jsonify({'state': 'idle'})
    if st.get('state') == 'running' and st.get('total'):
        try:
            with open('/run/uav-backup.progress') as f:
                raw = f.read().replace('\r', '\n')
            last = 0
            for line in raw.splitlines():
                m = re.match(r'\s*(\d+) bytes', line)
                if m:
                    last = int(m.group(1))
            if last:
                st['bytes'] = last
                st['percent'] = round(last * 100 / int(st['total']), 1)
        except (OSError, ValueError):
            pass
    return jsonify(st)


@app.route('/oled', methods=['POST'])
def oled_save():
    cfg = load_config()
    o = oled_config(cfg)
    o['enabled'] = request.form.get('enabled') == 'on'
    ctrl = request.form.get('controller', 'ssd1306')
    if ctrl in ('ssd1306', 'sh1106'):
        o['controller'] = ctrl
    addr = request.form.get('address', '0x3C')
    if addr in ('0x3C', '0x3D'):
        o['address'] = addr
    cfg['oled'] = o
    save_config(cfg)
    sh(['sudo', 'systemctl', 'restart', 'uav-oled.service'], timeout=15)
    return redirect('/')


@app.route('/msp', methods=['POST'])
def msp_save():
    cfg = load_config()
    m = msp_config(cfg)
    link = request.form.get('link', 'off')
    if link in ('off', 'uart', 'usb'):
        m['link'] = link
    try:
        baud = int(request.form.get('baud', 115200))
    except ValueError:
        baud = 115200
    if baud in MSP_BAUDS:
        m['baud'] = baud
    proto = request.form.get('protocol', 'msp')
    if proto in ('msp', 'mavlink'):
        m['protocol'] = proto
    try:
        port = int(request.form.get('udp_port', 5760))
    except ValueError:
        port = 5760
    if 0 < port <= 65535:
        m['udp_port'] = port
    m['inject_link_stats'] = request.form.get('inject') == 'on'
    m['arm_wifi_off'] = request.form.get('arm_wifi_off') == 'on'
    cfg['msp'] = m
    save_config(cfg)
    sh(['sudo', 'systemctl', 'restart', 'uav-msp.service'], timeout=30)
    return redirect('/')


@app.route('/apn', methods=['POST'])
def apn():
    new_apn = request.form.get('apn', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    if new_apn and not re.fullmatch(r'[A-Za-z0-9.\-]+', new_apn):
        return redirect('/')
    # leerer APN ist gueltig (Default-Bearer des Modems)
    args = ['sudo', 'nmcli', 'connection', 'modify', 'uav-wwan',
            'gsm.apn', new_apn]
    pin = request.form.get('pin', '').strip()
    if request.form.get('clear_pin') == '1':
        args += ['gsm.pin', '']
    elif pin and re.fullmatch(r'\d{4,8}', pin):
        args += ['gsm.pin', pin]
    if username:
        if re.fullmatch(r'[A-Za-z0-9.@\-]+', username):
            args += ['gsm.username', username]
            if password:
                args += ['gsm.password', password]
    else:
        args += ['gsm.username', '', 'gsm.password', '']
    sh(args)
    sh(['sudo', 'nmcli', 'connection', 'up', 'uav-wwan'], timeout=90)
    return redirect('/')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True)
