#!/usr/bin/env python3
"""UAV-Link OLED-Statusdisplay (I2C, SSD1306/SH1106, 128x64).

Isolierter, unkritischer Service: zeigt Link-/Systemstatus fuer den Feldaufbau.
Rendering (render()) ist reines PIL -> headless als PNG testbar.
Config: Abschnitt "oled" in config.json daneben.
"""
import glob
import json
import os
import re
import subprocess
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
OLED_DEFAULTS = {
    'enabled': True,
    'controller': 'ssd1306',   # ssd1306 (0.96") | sh1106 (1.3")
    'address': '0x3C',         # 0x3C | 0x3D
    'width': 128, 'height': 64,
}
ROTATE_S = 5.0


def sh(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


def load_cfg():
    cfg = dict(OLED_DEFAULTS)
    vid = {'width': 720, 'height': 576, 'framerate': 50}
    msp = {'protocol': 'msp', 'link': 'off', 'uart_device': '/dev/serial0'}
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        cfg.update(data.get('oled') or {})
        for k in vid:
            if k in data:
                vid[k] = data[k]
        msp.update(data.get('msp') or {})
    except (OSError, ValueError):
        pass
    return cfg, vid, msp


class Status:
    """Sammelt Anzeigewerte; langsame Quellen (mmcli/wg/nmcli) gecached."""

    def __init__(self):
        self.slow_ts = 0.0
        self.rx0 = self.tx0 = None
        self.rate_ts = 0.0
        self.d = {
            'tech': '', 'band': '', 'rsrp': None, 'lte_up': False,
            'vpn_ip': '', 'vpn_up': False,
            'wifi_name': '', 'wifi_ip': '', 'wifi_mode': 'off',
            'temp': 0.0, 'power_ok': True,
            'rx_bps': 0.0, 'tx_bps': 0.0,
        }

    def _fast(self):
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                self.d['temp'] = int(f.read().strip()) / 1000.0
        except (OSError, ValueError):
            pass
        m = re.search(r'0x([0-9a-fA-F]+)', sh(['vcgencmd', 'get_throttled']))
        thr = int(m.group(1), 16) if m else 0
        self.d['power_ok'] = not (thr & 0x1) and not (thr & 0x10000)
        # LTE-Datenrate aus /proc/net/dev
        rx = tx = None
        try:
            with open('/proc/net/dev') as f:
                for line in f:
                    if line.strip().startswith('wwan0:'):
                        p = line.split(':', 1)[1].split()
                        rx, tx = int(p[0]), int(p[8])
        except OSError:
            pass
        now = time.monotonic()
        if rx is not None and self.rx0 is not None and now > self.rate_ts:
            dt = now - self.rate_ts
            self.d['rx_bps'] = max(0.0, 8 * (rx - self.rx0) / dt)
            self.d['tx_bps'] = max(0.0, 8 * (tx - self.tx0) / dt)
        if rx is not None:
            self.rx0, self.tx0, self.rate_ts = rx, tx, now

    def _slow(self, msp):
        # LTE-Signal
        try:
            j = json.loads(sh(['mmcli', '-m', 'a', '--signal-get', '-J']))
            lte = j['modem']['signal'].get('lte') or {}
            self.d['rsrp'] = float(lte['rsrp']) if lte.get('rsrp') not in (None, '--') else None
        except (ValueError, KeyError, TypeError):
            self.d['rsrp'] = None
        try:
            gen = json.loads(sh(['mmcli', '-m', 'a', '-J']))['modem']['generic']
            techs = gen.get('access-technologies') or []
            t = techs[-1] if techs else ''
            self.d['tech'] = {'lte': '4G', '5gnr': '5G', 'umts': '3G'}.get(t, t.upper()[:3])
            self.d['lte_up'] = gen.get('state') == 'connected'
        except (ValueError, KeyError):
            self.d['tech'], self.d['lte_up'] = '', False
        m = re.search(r'eutran-(\d+)', sh(['qmicli', '-d', '/dev/cdc-wdm0', '-p',
                                          '--nas-get-rf-band-info']))
        self.d['band'] = f'B{m.group(1)}' if m else ''
        # VPN
        wg = sh(['wg', 'show', 'wgnet'])
        self.d['vpn_up'] = 'latest handshake' in wg and 'ago' in wg
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', sh(['ip', '-4', '-o', 'addr', 'show', 'wgnet']))
        self.d['vpn_ip'] = m.group(1) if m else ''
        # WiFi (client/AP/off)
        mode, conn = 'off', ''
        for line in sh(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION', 'device']).splitlines():
            p = line.split(':')
            if p[0] == 'wlan0' and len(p) >= 3 and p[1] == 'connected':
                conn = p[2]
                mode = 'ap' if conn == 'uav-hotspot' else 'client'
        if mode == 'ap':
            name = 'UAV-Link'
        elif mode == 'client':
            name = ''   # echte SSID der aktiven Verbindung
            for line in sh(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi']).splitlines():
                if line.startswith('yes:'):
                    name = line.split(':', 1)[1]
                    break
            if not name:
                name = conn   # Fallback: Profilname
        else:
            name = ''
        self.d['wifi_mode'], self.d['wifi_name'] = mode, name
        m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', sh(['ip', '-4', '-o', 'addr', 'show', 'wlan0']))
        self.d['wifi_ip'] = m.group(1) if m else ''
        # MSP/MAV-Link: Protokoll + FC-Port vorhanden?
        proto = (msp.get('protocol') or 'msp').upper()
        self.d['link_proto'] = 'MAV' if proto == 'MAVLINK' else 'MSP'
        link = msp.get('link', 'off')
        if link == 'uart':
            present = os.path.exists(msp.get('uart_device', '/dev/serial0'))
        elif link == 'usb':
            present = bool(glob.glob('/dev/ttyACM*'))
        else:
            present = False
        active = sh(['systemctl', 'is-active', 'uav-msp']).strip() == 'active'
        self.d['link_up'] = present and active

    def poll(self, msp, vid):
        self._fast()
        now = time.monotonic()
        if now - self.slow_ts > 3:
            self._slow(msp)
            self.slow_ts = now
        self.d['vid'] = vid
        return self.d


def fmt_rate(bps):
    if bps >= 1e6:
        return f'{bps/1e6:.1f}M'
    if bps >= 1e3:
        return f'{bps/1e3:.0f}k'
    return f'{bps:.0f}'


def render(draw, W, H, s, font, t):
    """Zeichnet das Layout (reines PIL). t = Sekunden (fuer Rotation)."""
    rot = int(t / ROTATE_S)
    row = [0, 11, 21, 32, 42, 53]

    def line(y, txt):
        draw.text((0, y), txt, font=font, fill=255)

    # Z1: LTE Signal + Balken
    rsrp = s['rsrp']
    line(row[0], f"LTE {s['tech'] or '--'} {s['band']}  "
                 f"{int(rsrp) if rsrp is not None else '--'}")
    # Signalbalken rechts (4 Balken je nach RSRP -113..-73)
    bars = 0 if rsrp is None else max(0, min(4, int((rsrp + 113) / 10)))
    bx, by = W - 22, 8
    for i in range(4):
        h = 2 + i * 2
        filled = i < bars
        draw.rectangle([bx + i * 5, by - h, bx + i * 5 + 3, by], outline=255,
                       fill=255 if filled else 0)

    # Z2: VPN
    line(row[1], f"VPN {'OK' if s['vpn_up'] else '--'}  {s['vpn_ip'] or '(down)'}")

    # Z3: WiFi rotiert Name <-> IP
    if s['wifi_mode'] == 'off':
        line(row[2], "WiFi: off")
    else:
        tag = 'AP' if s['wifi_mode'] == 'ap' else 'WiFi'
        if rot % 2 == 0:
            line(row[2], f"{tag}: {s['wifi_name'] or '-'}"[:21])
        else:
            line(row[2], f"{tag}: {s['wifi_ip'] or '-'}")

    # Z4: Video (fps <-> Aufloesung rotiert) + Link-Indikator
    vid = s['vid']
    vdet = f"{vid['framerate']}fps" if rot % 2 == 0 else f"{vid['width']}x{vid['height']}"
    link = f"{s.get('link_proto', 'MSP')} {'OK' if s.get('link_up') else '--'}"
    line(row[3], f"VID {vdet:<9} {link}")

    # Z5: Temp + Power
    line(row[4], f"CPU {s['temp']:.0f}C   "
                 f"Power {'OK' if s['power_ok'] else 'LOW'}")

    # Z6: nur LTE-Datenrate
    line(row[5], f"LTE u{fmt_rate(s['tx_bps'])} d{fmt_rate(s['rx_bps'])} bit/s")


def make_device(cfg):
    from luma.core.interface.serial import i2c
    addr = int(str(cfg['address']), 16)
    port_addrs = [(1, addr)]
    if addr == 0x3C:
        port_addrs.append((1, 0x3D))   # Fallback-Probe
    last = None
    for port, a in port_addrs:
        try:
            serial = i2c(port=port, address=a)
            if cfg['controller'] == 'sh1106':
                from luma.oled.device import sh1106
                return sh1106(serial, width=cfg['width'], height=cfg['height'])
            from luma.oled.device import ssd1306
            return ssd1306(serial, width=cfg['width'], height=cfg['height'])
        except Exception as e:
            last = e
    raise last


def main():
    from luma.core.render import canvas
    from PIL import ImageFont
    font = ImageFont.load_default()
    device = None
    warned = False
    st = Status()
    t0 = time.monotonic()
    while True:
        cfg, vid, msp = load_cfg()
        if not cfg['enabled']:
            time.sleep(3)
            continue
        if device is None:
            try:
                device = make_device(cfg)
                warned = False
                print(f'OLED bereit: {cfg["controller"]} @ {cfg["address"]}', flush=True)
            except Exception as e:
                if not warned:      # nur einmal melden, nicht im 3s-Takt spammen
                    print(f'OLED nicht gefunden ({e}) — warte auf Hardware', flush=True)
                    warned = True
                time.sleep(5)
                continue
        try:
            s = st.poll(msp, vid)
            with canvas(device) as draw:
                render(draw, cfg['width'], cfg['height'], s, font,
                       time.monotonic() - t0)
        except Exception as e:
            print(f'OLED-Fehler (weiter): {e!r}', flush=True)
            device = None       # Neu-Init beim naechsten Durchlauf
            time.sleep(2)
            continue
        time.sleep(1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
