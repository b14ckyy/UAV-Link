#!/usr/bin/env python3
"""UAV-Link MSP-DisplayPort-Reader (FPV-OSD).

Liest MSP-DisplayPort ("MSP OSD") von einem EIGENEN UART -- getrennt vom
Haupt-MSP-Link der Bridge, wie am FC verkabelt. Der FC ist Master und
schickt Zeichen-Kommandos (clear / write string / draw); dieser Dienst
haelt daraus das Zeichen-Grid (HD-Canvas 53x20, SD 30x16) und schiebt es
bei jedem "draw" als UDP-Paket an localhost -- dort rendert der
rtsp-server das Burn-in (osdstamp) und/oder reicht es zur GCS weiter.

Grid-Paket (UDP an 127.0.0.1:OSD_UDP_PORT, nur bei "draw"):
  b'UOSD' | version u8 | rows u8 | cols u8 | 0 u8 | rows*cols * u16le
  u16 = Glyphenindex (Zeichen + Fontpage*256), 0 = leere Zelle.

GCS-Downlink (UDP-Port DOWNLINK_PORT, IMMER aktiv, Spez: PROTOCOL-OSD.md):
Die GCS abonniert per 'OSUB'-Keepalive (1 Hz) an Port 5762; gesendet wird
an die beobachtete Absenderadresse (NAT-/WireGuard-tauglich). Jedes Paket
ist ein vollstaendiger, RLE-komprimierter Grid-Schnappschuss -- Verlust
ist egal, der naechste Draw ersetzt alles. Der Port ist API-Konstante
(hardcoded); die Web-UI verweigert ihn fuer den Serial-Tunnel.

Konfiguration (config.json, per Web-UI):
  "osd": { "enabled": bool, "mode": "burnin"|"downlink",
           "uart": "/dev/serial0", "baud": 115200 }
Baud ist bewusst NICHT im UI: der Port haengt fest am FC; 115200 traegt
HD-DisplayPort in der Praxis. Wie beim Recorder gilt: enabled=false ->
Exit 0, das Web-UI togglet per systemctl restart.

Status nach /run/uav-osd/status (JSON) fuers Web-UI.
"""
import json
import os
import select
import socket
import struct
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIR, 'config.json')
STATUS_PATH = '/run/uav-osd/status'
OSD_UDP_PORT = 5761       # intern (localhost -> rtsp-server/Burn-in)
DOWNLINK_PORT = 5762      # API-Konstante GCS-Downlink -- NICHT konfigurierbar
DOWNLINK_MAX = 1400       # max Payload/Paket (WireGuard-MTU ~1420)
SUB_TIMEOUT = 5.0         # Subscriber ohne Keepalive fliegen raus
HEARTBEAT = 1.0           # Downlink-Takt auch ohne FC-Draws

# MSP-DisplayPort-Subkommandos (Betaflight/INAV displayport_msp)
DP_HEARTBEAT = 0
DP_RELEASE = 1
DP_CLEAR = 2
DP_WRITE = 3
DP_DRAW = 4
DP_OPTIONS = 5
MSP_DISPLAYPORT = 182
MSP_FC_VARIANT = 2

# INAV sendet DisplayPort nur bei aktivem vtxActive-Gate: die Gegenseite muss
# binnen 1 s irgendein MSP-Kommando geschickt haben (displayport_msp_osd.c,
# VTX_TIMEOUT 1000 ms). Darum pollen wir wie HDZero/Walksnail zyklisch.
POLL_INTERVAL = 0.5
# MSPv1-Request '$M<' len=0: Checksumme = 0 ^ cmd = cmd
POLL_FRAME = bytes([ord('$'), ord('M'), ord('<'), 0,
                    MSP_FC_VARIANT, MSP_FC_VARIANT])

GRIDS = {0: (16, 30), 1: (18, 50), 2: (20, 53), 3: (20, 53)}
DEFAULT_GRID = (20, 53)


def log(msg):
    print(msg, flush=True)


def write_status(obj):
    try:
        with open(STATUS_PATH, 'w') as f:
            json.dump(obj, f)
        os.chmod(STATUS_PATH, 0o644)
    except OSError:
        pass


# --- MSP-Parser (Kopie aus msp-bridge.py; bewusst dupliziert: eigener ---------
# Prozess, und der Bindestrich im Dateinamen verhindert einen sauberen Import)
def crc8_dvb_s2(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class MspParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        return self.extract()

    def _resync(self):
        del self.buf[:1]

    def extract(self):
        items, buf = [], self.buf
        while buf:
            i = buf.find(b'$')
            if i < 0:
                buf.clear()
                break
            if i > 0:
                del buf[:i]
            if len(buf) < 3:
                break
            if buf[2] not in b'<>!':
                self._resync()
                continue
            if buf[1] == ord('X'):                      # MSPv2
                if len(buf) < 8:
                    break
                func, size = struct.unpack_from('<HH', buf, 4)
                if size > 8192:
                    self._resync()
                    continue
                total = 8 + size + 1
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                if crc8_dvb_s2(frame[3:-1]) == frame[-1]:
                    items.append((func, frame[8:-1]))
                    del buf[:total]
                else:
                    self._resync()
            elif buf[1] == ord('M'):                    # MSPv1 (+ Jumbo)
                if len(buf) < 5:
                    break
                size, cmd, hdr = buf[3], buf[4], 5
                if size == 255:
                    if len(buf) < 7:
                        break
                    size, hdr = struct.unpack_from('<H', buf, 5)[0], 7
                if size > 8192:
                    self._resync()
                    continue
                total = hdr + size + 1
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                csum = 0
                for b in frame[3:-1]:
                    csum ^= b
                if csum == frame[-1]:
                    items.append((cmd, frame[hdr:-1]))
                    del buf[:total]
                else:
                    self._resync()
            else:
                self._resync()
        return items


# --- Serial (termios, Stil wie msp-bridge) ------------------------------------
def open_serial(dev, baud):
    import termios
    import tty
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, f'B{baud}', termios.B115200)
    attrs[4] = attrs[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


class OsdState:
    def __init__(self):
        self.rows, self.cols = DEFAULT_GRID
        self.grid = [0] * (self.rows * self.cols)
        self.draws = 0
        self.last_draw = 0.0
        self.last_frame = 0.0

    def set_grid(self, rows, cols):
        if (rows, cols) != (self.rows, self.cols):
            log(f'Grid: {cols}x{rows}')
            self.rows, self.cols = rows, cols
            self.grid = [0] * (rows * cols)

    def clear(self):
        self.grid = [0] * (self.rows * self.cols)

    def write(self, row, col, attr, chars):
        # Fontpage in den Attr-Bits (Bit 0/1); wir tragen 2 Seiten = 512 Glyphen
        page = attr & 0x01
        if row >= self.rows:
            return
        base = row * self.cols
        for i, ch in enumerate(chars):
            c = col + i
            if c >= self.cols:
                break
            self.grid[base + c] = ch + 256 * page

    def packet(self):
        return (b'UOSD' + bytes([1, self.rows, self.cols, 0])
                + struct.pack(f'<{len(self.grid)}H', *self.grid))


class Downlink:
    """GCS-Abo-Verwaltung + Grid-Snapshots per UDP (Spez: PROTOCOL-OSD.md).

    Immer aktiv: gesendet wird nur an Adressen, die per 'OSUB'-Keepalive
    abonniert haben -- ohne Abo kostet das hier nichts.
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('', DOWNLINK_PORT))
        self.sock.setblocking(False)
        self.subs = {}                 # addr -> letzter Keepalive
        self.seq = 0
        self._last = None              # zuletzt gesendetes Grid

    def poll_subs(self, now):
        try:
            while True:
                data, addr = self.sock.recvfrom(64)
                if data[:4] == b'OSUB':
                    if addr not in self.subs:
                        log(f'GCS abonniert: {addr[0]}:{addr[1]}')
                    self.subs[addr] = now
        except (BlockingIOError, InterruptedError):
            pass
        for a in [a for a, t in self.subs.items()
                  if now - t > SUB_TIMEOUT]:
            del self.subs[a]
            log(f'GCS-Abo abgelaufen: {a[0]}:{a[1]}')

    @staticmethod
    def _rle(cells):
        # u16-Tokens: 0xFFFF-Escape + Anzahl Nullzellen, sonst Literal.
        out = []
        i, n = 0, len(cells)
        while i < n:
            if cells[i]:
                out.append(cells[i])
                i += 1
            else:
                j = i + 1
                while j < n and not cells[j]:
                    j += 1
                out.append(0xFFFF)
                out.append(j - i)
                i = j
        return out

    def send(self, state, fc_alive, force=False):
        # INAV zeichnet ~46x/s, der Inhalt aendert sich aber nur selten --
        # unveraenderte Grids kosten sonst grundlos ~190 kbit/s. Der 1-Hz-
        # Heartbeat (force=True) traegt die Liveness, nicht die Draw-Rate.
        if not self.subs:
            return
        cur = tuple(state.grid)
        if not force and cur == self._last:
            return
        self._last = cur
        self.seq = (self.seq + 1) & 0xFFFF
        toks = self._rle(state.grid)
        if 14 + 2 * len(toks) <= DOWNLINK_MAX:
            stripes = [(0, state.rows, toks)]
        else:
            # Sehr dichtes Grid: als zwei Zeilenstreifen (je < MTU)
            half = state.rows // 2
            stripes = []
            for r0, nr in ((0, half), (half, state.rows - half)):
                seg = state.grid[r0 * state.cols:(r0 + nr) * state.cols]
                stripes.append((r0, nr, self._rle(seg)))
        for r0, nr, t in stripes:
            pkt = (struct.pack('<4sBBBBHBBBB', b'UOSD', 1, 1,
                               state.rows, state.cols, self.seq,
                               r0, nr, 1 if fc_alive else 0, 0)
                   + struct.pack(f'<{len(t)}H', *t))
            for addr in list(self.subs):
                try:
                    self.sock.sendto(pkt, addr)
                except OSError:
                    pass


def main():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    osd = cfg.get('osd') or {}
    if not osd.get('enabled'):
        write_status({'state': 'disabled'})
        log('OSD deaktiviert -- Ende')
        return 0
    dev = osd.get('uart', '/dev/serial0')
    baud = int(osd.get('baud', 115200))

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dlink = Downlink()
    state = OsdState()
    parser = MspParser()
    fd = None
    last_status = 0.0
    last_poll = 0.0
    last_hb = 0.0
    last_open = -10.0
    fc_variant = None
    draws_window = []

    log(f'MSP-DisplayPort-Reader: {dev} @ {baud}, '
        f'GCS-Downlink auf UDP {DOWNLINK_PORT}')
    while True:
        now = time.monotonic()
        # UART (wieder) oeffnen -- nicht blockierend warten, der Downlink
        # (Abos + Heartbeat) laeuft auch ohne FC weiter.
        if fd is None and now - last_open >= 3.0:
            last_open = now
            try:
                fd = open_serial(dev, baud)
                log(f'UART offen: {dev}')
            except OSError as e:
                write_status({'state': 'no-uart', 'uart': dev,
                              'error': str(e)})
        rl = [dlink.sock] if fd is None else [fd, dlink.sock]
        r, _, _ = select.select(rl, [], [], 0.5)
        now = time.monotonic()
        dlink.poll_subs(now)
        if fd is not None and now - last_poll >= POLL_INTERVAL:
            last_poll = now
            try:
                os.write(fd, POLL_FRAME)
            except OSError:
                os.close(fd)
                fd = None
                log('UART-Schreibfehler -- neu verbinden')
                continue
        if fd is not None and fd in r:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b''
            if not data:
                os.close(fd)
                fd = None
                log('UART weg -- neu verbinden')
                continue
            state.last_frame = now
            for func, payload in parser.feed(data):
                if func == MSP_FC_VARIANT and payload and fc_variant is None:
                    fc_variant = payload.decode('ascii', 'replace')
                    log(f'FC erkannt: {fc_variant}')
                if func != MSP_DISPLAYPORT or not payload:
                    continue
                sub = payload[0]
                if sub == DP_CLEAR:
                    state.clear()
                elif sub == DP_WRITE and len(payload) >= 4:
                    state.write(payload[1], payload[2], payload[3],
                                payload[4:])
                elif sub == DP_DRAW:
                    state.draws += 1
                    state.last_draw = now
                    draws_window.append(now)
                    sock.sendto(state.packet(),
                                ('127.0.0.1', OSD_UDP_PORT))
                    dlink.send(state, True)
                elif sub == DP_OPTIONS and len(payload) >= 3:
                    state.set_grid(*GRIDS.get(payload[2], DEFAULT_GRID))
        if now - last_hb >= HEARTBEAT:
            last_hb = now
            dlink.send(state, now - state.last_draw < 3.0, force=True)
        if now - last_status >= 2.0:
            last_status = now
            draws_window = [t for t in draws_window if now - t < 5.0]
            write_status({
                'state': ('active' if now - state.last_draw < 3.0 else
                          'waiting'),
                'uart': dev,
                'grid': f'{state.cols}x{state.rows}',
                'draw_hz': round(len(draws_window) / 5.0, 1),
                'mode': osd.get('mode', 'burnin'),
                'fc': fc_variant,
                'gcs_subs': len(dlink.subs),
            })


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
