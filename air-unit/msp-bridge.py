#!/usr/bin/env python3
"""UAV-Link Serial-Bridge: FC (UART/USB-VCP) <-> Bodenstation (UDP).

Protokoll-agnostischer Kern (Transport, Byte/Frame-Relay, GCS-Prioritaet) mit
austauschbarem Protokoll-Modul (MSP oder MAVLink). Design-Prinzip TRANSPARENZ:
Die GCS<->FC-Konversation wird nie gestoert, die GCS sieht nie unseren Traffic.

- MSP: eigene Requests tragen das ILMI-Flag (0x02); der FC spiegelt es in der
  Reply -> wir filtern ILMI-Replies aus dem FC->GCS-Strom, die GCS sieht sie nie.
  LTE-Linkdaten als NO_REPLY-Push (RC_LINK_STATS/RC_INFO). Arm-Status aus
  MSP2_INAV_STATUS (passiv mitgehoert; im GCS-losen Betrieb selbst per ILMI gepollt).
- MAVLink: rein passiv mithoeren (HEARTBEAT -> Arm), RADIO_STATUS-Push Richtung
  GCS (normales Funkmodem-Verhalten). Kein ILMI-Aequivalent.
- Lueckenfueller-Statuspoll NUR wenn keine GCS verbunden ist (Hysterese).
- Bei Arm: nach Delay WiFi aus, LTE/VPN bleibt (config arm_wifi_off).

Config: Abschnitt "msp" in config.json (daneben), Aenderung ueber die Web-UI.
"""
import glob
import json
import os
import re
import selectors
import socket
import struct
import subprocess
import sys
import threading
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
MSP_DEFAULTS = {
    'link': 'off',              # off | uart | usb
    'uart_device': '/dev/serial0',
    'baud': 115200,
    'udp_port': 5760,
    'protocol': 'msp',          # msp | mavlink
    'inject_link_stats': True,  # LTE-Linkdaten injizieren
    'poll_status': True,        # Lueckenfueller-Statuspoll (nur ohne GCS)
    'arm_wifi_off': False,      # WiFi bei Arm abschalten (Validierung: default aus)
    'arm_wifi_delay': 30,       # Sekunden Arm bis WiFi-Abschaltung
}

# --- MSP ---
MSP_FLAG_NO_REPLY = 0x01
MSP_FLAG_ILMI = 0x02
MSP2_INAV_STATUS = 0x2000
MSP_STATUS_EX = 150
INAV_ARMED_BIT = 0x04
MSP2_SET_RC_LINK_STATS = 0x100D
MSP2_SET_RC_INFO = 0x100E

# --- MAVLink ---
MAV_STX_V1 = 0xFE
MAV_STX_V2 = 0xFD
MAV_MSG_HEARTBEAT = 0
MAV_MSG_RADIO_STATUS = 109
MAV_MSG_REQUEST_DATA_STREAM = 66
MAV_TYPE_GCS = 6
MAV_MODE_SAFETY_ARMED = 0x80
MAV_DATA_STREAM_ALL = 0
CRC_EXTRA = {MAV_MSG_HEARTBEAT: 50, MAV_MSG_RADIO_STATUS: 185,
             MAV_MSG_REQUEST_DATA_STREAM: 148}

# --- Timing ---
STATS_PERIOD = 1.0
INFO_PERIOD = 5.0
POLL_PERIOD = 1.0
GCS_PRESENT_S = 3.0     # Hysterese: GCS gilt als da, wenn Traffic juenger als das
GCS_ALIVE_S = 1.5       # validLink nur bei kuerzlichem GCS-Traffic
FLUSH_S = 0.2
BAND_BY_TECH = {'5gnr': '5G', 'lte': '4G', 'umts': '3G', 'hsdpa': '3G',
                'hsupa': '3G', 'hspa': '3G', 'hspa-plus': '3G',
                'edge': '2G', 'gprs': '2G', 'gsm': '2G'}


def log(msg):
    print(msg, flush=True)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


_notify_sock = None
_notify_addr = None


def sd_notify(state):
    """systemd sd_notify (READY=1 / WATCHDOG=1). No-op ohne NOTIFY_SOCKET."""
    global _notify_sock, _notify_addr
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr:
        return
    try:
        if _notify_sock is None:
            _notify_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _notify_addr = ('\0' + addr[1:]) if addr[0] == '@' else addr
        _notify_sock.sendto(state.encode(), _notify_addr)
    except OSError:
        pass


# ===================== MSP =====================

def crc8_dvb_s2(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def msp2(func, payload=b'', flag=0):
    body = bytes([flag]) + struct.pack('<HH', func, len(payload)) + payload
    return b'$X<' + body + bytes([crc8_dvb_s2(body)])


class MspParser:
    """Inkrementeller MSP-v1/v2-Frame-Extraktor mit Raw-Durchreichung.

    extract() liefert ('frame', func, dir, bytes) fuer gueltige Frames und
    ('raw', None, None, bytes) fuer alles, was kein MSP ist (CLI-Text etc.).
    """

    def __init__(self):
        self.buf = bytearray()
        self.last_feed = 0.0

    def feed(self, data):
        self.buf += data
        self.last_feed = time.monotonic()
        return self.extract()

    def flush(self):
        raw = bytes(self.buf)
        self.buf.clear()
        return raw

    def at_boundary(self):
        return not self.buf

    def _resync(self, items):
        items.append(('raw', None, None, bytes(self.buf[:1])))
        del self.buf[:1]

    def extract(self):
        items, buf = [], self.buf
        while buf:
            i = buf.find(b'$')
            if i < 0:
                items.append(('raw', None, None, bytes(buf)))
                buf.clear()
                break
            if i > 0:
                items.append(('raw', None, None, bytes(buf[:i])))
                del buf[:i]
            if len(buf) < 3:
                break
            if buf[2] not in b'<>!':
                self._resync(items)
                continue
            if buf[1] == ord('X'):                      # MSPv2
                if len(buf) < 8:
                    break
                func, size = struct.unpack_from('<HH', buf, 4)
                if size > 8192:
                    self._resync(items)
                    continue
                total = 8 + size + 1
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                if crc8_dvb_s2(frame[3:-1]) == frame[-1]:
                    items.append(('frame', func, chr(frame[2]), frame))
                    del buf[:total]
                else:
                    self._resync(items)
            elif buf[1] == ord('M'):                    # MSPv1 (+ Jumbo)
                if len(buf) < 5:
                    break
                size, cmd, hdr = buf[3], buf[4], 5
                if size == 255:
                    if len(buf) < 7:
                        break
                    size, hdr = struct.unpack_from('<H', buf, 5)[0], 7
                if size > 8192:
                    self._resync(items)
                    continue
                total = hdr + size + 1
                if len(buf) < total:
                    break
                frame = bytes(buf[:total])
                csum = 0
                for b in frame[3:-1]:
                    csum ^= b
                if csum == frame[-1]:
                    items.append(('frame', cmd, chr(frame[2]), frame))
                    del buf[:total]
                else:
                    self._resync(items)
            else:
                self._resync(items)
        return items


def msp_payload(frame):
    """Payload aus einem MSP-v1/v2-Frame (ohne Header/CRC)."""
    if frame[1] == ord('X'):
        return frame[8:-1]
    size = frame[3]
    if size == 255:
        return frame[7:-1]
    return frame[5:-1]


class MspProtocol:
    name = 'msp'
    inject_dest = 'fc'          # LTE-Linkdaten gehen zum FC

    def __init__(self):
        self.fc = MspParser()
        self.gcs = MspParser()
        self.armed = None
        self.last_gcs_raw = 0.0   # CLI-Verdacht (Raw-Traffic von der GCS)

    # FC -> GCS: ILMI-Replies (unsere) rausfiltern, Arm mitlesen
    def process_fc(self, data):
        out = bytearray()
        for kind, func, dirn, frame in self.fc.feed(data):
            if kind == 'raw':
                out += frame
                continue
            is_ours = frame[:2] == b'$X' and (frame[3] & MSP_FLAG_ILMI)
            self._read_status(func, frame)
            if is_ours:
                continue          # unsere ILMI-Reply -> nicht an die GCS
            out += frame
        return bytes(out)

    def flush_fc(self):
        return self.fc.flush()

    def fc_at_boundary(self):
        return self.fc.at_boundary()

    def fc_last_feed(self):
        return self.fc.last_feed

    # GCS -> FC: nur durchreichen; CLI-Verdacht (Raw) merken
    def process_gcs(self, data):
        for kind, *_ in self.gcs.feed(data):
            if kind == 'raw':
                self.last_gcs_raw = time.monotonic()
        return data

    def gcs_at_boundary(self):
        return self.gcs.at_boundary()

    def gcs_last_feed(self):
        return self.gcs.last_feed

    def flush_gcs(self):
        return self.gcs.flush()

    def cli_suspected(self):
        return time.monotonic() - self.last_gcs_raw < 10

    def _read_status(self, func, frame):
        try:
            if frame[1] == ord('X') and func == MSP2_INAV_STATUS:
                p = msp_payload(frame)
                if len(p) >= 13:
                    flags = struct.unpack_from('<I', p, 9)[0]
                    self.armed = bool(flags & INAV_ARMED_BIT)
            elif func == MSP_STATUS_EX:
                p = msp_payload(frame)
                if len(p) >= 15:
                    flags = struct.unpack_from('<H', p, 13)[0]
                    self.armed = bool(flags & INAV_ARMED_BIT)
        except (struct.error, IndexError):
            pass

    # Lueckenfueller-Poll (nur ohne GCS): INAV_STATUS mit ILMI-Flag
    def build_poll(self):
        return msp2(MSP2_INAV_STATUS, b'', flag=MSP_FLAG_ILMI)

    # LTE-Linkdaten als NO_REPLY-Push
    def build_injections(self, lte, gcs_alive):
        msgs = []
        pct = clamp(round((lte.rsrp + 115) * 100 / 30), 0, 100) if lte.rsrp is not None else 0
        dbm = clamp(round(-lte.rsrp), 0, 255) if lte.rsrp is not None else 0
        snr = clamp(round(lte.snr), -128, 127) if lte.snr is not None else 0
        dlq = clamp(round(lte.rx_bps / 1e5), 0, 100)
        ulq = clamp(round(lte.tx_bps / 1e5), 0, 100)
        valid = 1 if (gcs_alive and lte.connected) else 0
        stats = struct.pack('<BBBBBBb', 0, valid, pct, dbm, dlq, ulq, snr)
        msgs.append(msp2(MSP2_SET_RC_LINK_STATS, stats, flag=MSP_FLAG_NO_REPLY))
        return msgs

    def build_info(self, lte):
        band = BAND_BY_TECH.get(lte.tech or '', '').encode().ljust(4, b'\0')[:4]
        mode = (lte.band or '').encode()[:6].ljust(6, b'\0')
        payload = struct.pack('<BHH', 0, 0, 0) + band + mode
        return msp2(MSP2_SET_RC_INFO, payload, flag=MSP_FLAG_NO_REPLY)


# ===================== MAVLink =====================

def crc16_x25(data, crc=0xFFFF):
    for b in data:
        tmp = (b ^ (crc & 0xFF)) & 0xFF
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def mav_v1(msgid, payload, seq, sysid, compid):
    """MAVLink-v1-Frame (0xFE) mit korrektem X25-CRC inkl. CRC_EXTRA."""
    hdr = struct.pack('<BBBBB', len(payload), seq & 0xFF, sysid, compid, msgid)
    crc = crc16_x25(hdr + payload)
    crc = crc16_x25(bytes([CRC_EXTRA[msgid]]), crc)
    return b'\xfe' + hdr + payload + struct.pack('<H', crc)


class MavlinkSniffer:
    """Leichter MAVLink-v1/v2-Frame-Extraktor. Liefert (msgid, payload) fuer
    Frames, deren CRC_EXTRA wir kennen (validiert); der Rest wird per LEN
    uebersprungen (Framing). Fuer reines Mithoeren (Arm/HEARTBEAT)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out, buf = [], self.buf
        while buf:
            if buf[0] == MAV_STX_V1:
                if len(buf) < 8:
                    break
                ln = buf[1]
                total = 6 + ln + 2
                if len(buf) < total:
                    break
                sysid = buf[3]
                msgid = buf[5]
                payload = bytes(buf[6:6 + ln])
                crc = struct.unpack_from('<H', buf, 6 + ln)[0]
                self._emit(out, msgid, sysid, payload, buf[1:6 + ln], crc)
                del buf[:total]
            elif buf[0] == MAV_STX_V2:
                if len(buf) < 12:
                    break
                ln = buf[1]
                incompat = buf[2]
                signed = 13 if (incompat & 0x01) else 0
                total = 10 + ln + 2 + signed
                if len(buf) < total:
                    break
                sysid = buf[5]
                msgid = buf[7] | (buf[8] << 8) | (buf[9] << 16)
                payload = bytes(buf[10:10 + ln])
                crc = struct.unpack_from('<H', buf, 10 + ln)[0]
                self._emit(out, msgid, sysid, payload, buf[1:10 + ln], crc)
                del buf[:total]
            else:
                del buf[:1]
        return out

    def _emit(self, out, msgid, sysid, payload, hdr_and_payload, crc):
        if msgid in CRC_EXTRA:
            c = crc16_x25(bytes(hdr_and_payload))
            c = crc16_x25(bytes([CRC_EXTRA[msgid]]), c)
            if c != crc:
                return   # CRC passt nicht -> verwerfen (Fehl-Sync)
        else:
            return       # unbekannte msgid: nur ueberspringen, nicht auswerten
        out.append((msgid, sysid, payload))


class MavlinkProtocol:
    name = 'mavlink'
    inject_dest = 'gcs'          # RADIO_STATUS geht zur GCS

    def __init__(self):
        self.sniff = MavlinkSniffer()
        self.armed = None
        self.fc_sysid = None
        self.seq = 0

    # FC -> GCS: transparent durchreichen; HEARTBEAT mitlesen
    def process_fc(self, data):
        for msgid, sysid, payload in self.sniff.feed(data):
            if msgid == MAV_MSG_HEARTBEAT and len(payload) >= 7:
                # HEARTBEAT-Wire: custom_mode(4), type,autopilot,base_mode,...
                mtype = payload[4]
                base_mode = payload[6]
                if mtype != MAV_TYPE_GCS:        # der FC, nicht ein GCS-Heartbeat
                    self.fc_sysid = sysid
                    self.armed = bool(base_mode & MAV_MODE_SAFETY_ARMED)
        return data                              # reiner Byte-Passthrough

    def flush_fc(self):
        return b''

    def fc_at_boundary(self):
        return True                              # kein Boundary-Halten noetig

    def fc_last_feed(self):
        return 0.0

    def process_gcs(self, data):
        return data                              # reiner Byte-Passthrough

    def gcs_at_boundary(self):
        return True

    def gcs_last_feed(self):
        return 0.0

    def flush_gcs(self):
        return b''

    def cli_suspected(self):
        return False

    # Lueckenfueller (nur ohne GCS): Standard-Streams anfordern
    def build_poll(self):
        if self.fc_sysid is None:
            return None                          # FC-sysid noch unbekannt
        self.seq = (self.seq + 1) & 0xFF
        # REQUEST_DATA_STREAM Wire-Order (u16 zuerst): req_message_rate(2),
        #   target_system, target_component, req_stream_id, start_stop
        payload = struct.pack('<HBBBB', 2, self.fc_sysid, 1, MAV_DATA_STREAM_ALL, 1)
        return mav_v1(MAV_MSG_REQUEST_DATA_STREAM, payload, self.seq, 200, 190)

    # RADIO_STATUS Richtung GCS (normales Funkmodem-Verhalten)
    def build_injections(self, lte, gcs_alive):
        rssi = clamp(round((lte.rsrp + 115) * 255 / 30), 0, 254) if lte.rsrp is not None else 0
        noise = clamp(round(30 - lte.snr), 0, 254) if lte.snr is not None else 0
        txbuf = 100
        # RADIO_STATUS-Wire: rxerrors(u16), fixed(u16), rssi, remrssi, txbuf, noise, remnoise
        payload = struct.pack('<HHBBBBB', 0, 0, rssi, 0, txbuf, noise, 0)
        self.seq = (self.seq + 1) & 0xFF
        return [mav_v1(MAV_MSG_RADIO_STATUS, payload, self.seq, 51, 68)]

    def build_info(self, lte):
        return None


# ===================== LTE-Monitor =====================

class LteMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.rsrp = self.snr = None
        self.tech = ''
        self.band = ''
        self.rx_bps = self.tx_bps = 0.0
        self.connected = False

    @staticmethod
    def _run(cmd, timeout=10):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ''

    @staticmethod
    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _counters(self):
        try:
            with open('/proc/net/dev') as f:
                for line in f:
                    if line.strip().startswith('wwan0:'):
                        fields = line.split(':', 1)[1].split()
                        return int(fields[0]), int(fields[8])
        except OSError:
            pass
        return None

    def run(self):
        prev, prev_ts, cycle = None, 0.0, 0
        while True:
            try:
                prev, prev_ts = self._poll(cycle, prev, prev_ts)
            except Exception as e:   # Thread darf NIE sterben (nur Daten veralten)
                log(f'LteMonitor-Fehler (mache weiter): {e!r}')
            cycle += 1
            time.sleep(2)

    def _poll(self, cycle, prev, prev_ts):
        if cycle % 30 == 0:
            self._run(['mmcli', '-m', 'a', '--signal-setup=5'])
        out = self._run(['mmcli', '-m', 'a', '--signal-get', '-J'])
        try:
            lte = (json.loads(out)['modem']['signal'].get('lte') or {})
            self.rsrp = self._num(lte.get('rsrp'))
            self.snr = self._num(lte.get('snr'))
        except (ValueError, KeyError):
            self.rsrp = self.snr = None
        if cycle % 5 == 0:
            try:
                gen = json.loads(self._run(['mmcli', '-m', 'a', '-J']))['modem']['generic']
                techs = gen.get('access-technologies') or []
                self.tech = techs[-1] if techs else ''
                self.connected = gen.get('state') == 'connected'
            except (ValueError, KeyError):
                self.tech, self.connected = '', False
            m = re.search(r'eutran-(\d+)',
                          self._run(['qmicli', '-d', '/dev/cdc-wdm0', '-p',
                                     '--nas-get-rf-band-info']))
            self.band = f'B{m.group(1)}' if m else ''
        cnt, now = self._counters(), time.monotonic()
        if cnt and prev and now > prev_ts:
            dt = now - prev_ts
            self.rx_bps = max(0.0, 8 * (cnt[0] - prev[0]) / dt)
            self.tx_bps = max(0.0, 8 * (cnt[1] - prev[1]) / dt)
        if cnt:
            prev, prev_ts = cnt, now
        return prev, prev_ts


# ===================== Bridge =====================

class Bridge:
    def __init__(self, cfg, proto):
        self.cfg = cfg
        self.proto = proto
        self.sel = selectors.DefaultSelector()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # DSCP EF
        self.udp.bind(('', cfg['udp_port']))
        self.udp.setblocking(False)
        self.sel.register(self.udp, selectors.EVENT_READ, 'udp')
        self.ser_fd = None
        self.ser_buf = bytearray()
        self.gcs_addr = None
        self.last_gcs = 0.0        # letzter GCS->FC-Traffic (Hysterese)
        self.mon = LteMonitor()
        self.mon.start()
        # Arm/WiFi-Zustandsmaschine
        self.armed = False
        self.arm_since = 0.0
        self.wifi_off_done = False

    # --- Serial ---
    def _serial_device(self):
        if self.cfg['link'] == 'uart':
            return self.cfg['uart_device']
        acm = sorted(glob.glob('/dev/ttyACM*'))
        return acm[0] if acm else None

    def open_serial(self):
        import termios
        import tty
        dev = self._serial_device()
        if not dev or not os.path.exists(dev):
            return
        try:
            fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f'B{self.cfg["baud"]}', termios.B115200)
            attrs[4] = attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except (OSError, termios.error) as e:
            log(f'Serial {dev} nicht nutzbar: {e}')
            return
        self.ser_fd = fd
        self.ser_buf.clear()
        self.sel.register(fd, selectors.EVENT_READ, 'ser')
        log(f'FC-Port offen: {dev} @ {self.cfg["baud"]} Baud ({self.cfg["link"]}, '
            f'{self.proto.name})')

    def close_serial(self, why):
        if self.ser_fd is None:
            return
        log(f'FC-Port zu: {why}')
        try:
            self.sel.unregister(self.ser_fd)
        except KeyError:
            pass
        try:
            os.close(self.ser_fd)
        except OSError:
            pass
        self.ser_fd = None
        self.ser_buf.clear()

    def _update_ser_events(self):
        if self.ser_fd is None:
            return
        ev = selectors.EVENT_READ
        if self.ser_buf:
            ev |= selectors.EVENT_WRITE
        self.sel.modify(self.ser_fd, ev, 'ser')

    def kick_serial(self):
        while self.ser_buf and self.ser_fd is not None:
            try:
                n = os.write(self.ser_fd, self.ser_buf)
                del self.ser_buf[:n]
            except BlockingIOError:
                break
            except OSError as e:
                self.close_serial(f'Schreibfehler ({e})')
                return
        self._update_ser_events()

    def to_fc(self, data):
        if self.ser_fd is not None and data:
            self.ser_buf += data
            self.kick_serial()

    def to_gcs(self, data):
        if data and self.gcs_addr is not None:
            try:
                self.udp.sendto(data, self.gcs_addr)
            except OSError:
                pass

    # --- Datenfluss ---
    def on_udp(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(65535)
            except (BlockingIOError, OSError):
                return
            if addr != self.gcs_addr:
                log(f'GCS: {addr[0]}:{addr[1]}')
                self.gcs_addr = addr
            self.last_gcs = time.monotonic()
            self.to_fc(self.proto.process_gcs(data))

    def on_serial_read(self):
        while self.ser_fd is not None:
            try:
                data = os.read(self.ser_fd, 4096)
            except BlockingIOError:
                return
            except OSError as e:
                self.close_serial(f'Lesefehler ({e})')
                return
            if not data:
                self.close_serial('EOF')
                return
            self.to_gcs(self.proto.process_fc(data))

    def gcs_present(self):
        return time.monotonic() - self.last_gcs < GCS_PRESENT_S

    def gcs_alive(self):
        return time.monotonic() - self.last_gcs < GCS_ALIVE_S

    # --- Arm/WiFi-Zustandsmaschine ---
    def update_arm(self):
        armed = bool(self.proto.armed)
        now = time.monotonic()
        if armed and not self.armed:
            self.armed = True
            self.arm_since = now
            log('ARM erkannt')
        elif not armed and self.armed:
            self.armed = False
            log('DISARM erkannt')
            if self.wifi_off_done:
                self.set_wifi(True)
                self.wifi_off_done = False
        delay = self.cfg['arm_wifi_delay']
        if (self.armed and not self.wifi_off_done
                and now - self.arm_since >= delay):
            if self.cfg['arm_wifi_off']:
                log(f'ARM seit {delay:.0f}s -> WiFi aus (LTE bleibt)')
                self.set_wifi(False)
            else:
                log(f'ARM seit {delay:.0f}s -> WiFi WUERDE aus '
                    f'(arm_wifi_off=false, Validierung)')
            self.wifi_off_done = True

    def set_wifi(self, on):
        # asynchron: darf die Hauptschleife (und den Watchdog) nie blockieren
        def _do():
            try:
                subprocess.run(['nmcli', 'radio', 'wifi', 'on' if on else 'off'],
                               capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as e:
                log(f'nmcli radio wifi fehlgeschlagen: {e}')
        threading.Thread(target=_do, daemon=True).start()

    # --- Injection/Poll ---
    def can_inject_fc(self):
        return (self.ser_fd is not None and not self.ser_buf
                and self.proto.fc_at_boundary() and self.proto.gcs_at_boundary()
                and not self.proto.cli_suspected())

    def inject(self, dest, msg):
        if not msg:
            return
        if dest == 'fc':
            self.to_fc(msg)
        else:
            if self.gcs_present():          # RADIO_STATUS nur wenn GCS da
                self.to_gcs(msg)

    # --- Hauptschleife ---
    def run(self):
        now = time.monotonic()
        self.next_stats = now + 2
        self.next_info = now + 3
        self.next_poll = now + 2
        self.next_retry = now
        self.next_arm = now + 1
        log(f'Serial-Bridge laeuft: udp://0.0.0.0:{self.cfg["udp_port"]} <-> '
            f'{self.cfg["link"]} [{self.proto.name}] '
            f'(inject={"an" if self.cfg["inject_link_stats"] else "aus"}, '
            f'poll={"an" if self.cfg["poll_status"] else "aus"}, '
            f'arm_wifi_off={"an" if self.cfg["arm_wifi_off"] else "aus"})')
        sd_notify('READY=1')
        errors = 0
        while True:
            try:
                sd_notify('WATCHDOG=1')
                self._tick()
            except Exception as e:
                # KEIN einzelner Fehler darf den Prozess killen -> loggen, weiter
                errors += 1
                if errors <= 5 or errors % 100 == 0:
                    log(f'Loop-Fehler #{errors} (mache weiter): {e!r}')
                time.sleep(0.1)

    def _tick(self):
        now = time.monotonic()
        if self.ser_fd is None and now >= self.next_retry:
            self.open_serial()
            self.next_retry = now + 2
        deadlines = [self.next_stats, self.next_info, self.next_poll, self.next_arm]
        if self.ser_fd is None:
            deadlines.append(self.next_retry)
        if not self.proto.fc_at_boundary():
            deadlines.append(self.proto.fc_last_feed() + FLUSH_S)
        timeout = clamp(min(deadlines) - now, 0, 1)
        for key, mask in self.sel.select(timeout):
            if key.data == 'udp':
                self.on_udp()
            elif key.data == 'ser':
                if mask & selectors.EVENT_READ:
                    self.on_serial_read()
                if mask & selectors.EVENT_WRITE:
                    self.kick_serial()
        now = time.monotonic()
        # haengende Teilframes/Nicht-MSP-Bytes roh weiterreichen
        if (not self.proto.fc_at_boundary()
                and now - self.proto.fc_last_feed() > FLUSH_S):
            self.to_gcs(self.proto.flush_fc())
        # Arm/WiFi
        if now >= self.next_arm:
            self.next_arm = now + 0.5
            self.update_arm()
        # Injection LTE-Linkdaten
        if now >= self.next_stats:
            self.next_stats = now + STATS_PERIOD
            if self.cfg['inject_link_stats'] and self.ser_fd is not None:
                dest = self.proto.inject_dest
                ok = self.can_inject_fc() if dest == 'fc' else True
                if ok:
                    for msg in self.proto.build_injections(self.mon, self.gcs_alive()):
                        self.inject(dest, msg)
        if now >= self.next_info:
            self.next_info = now + INFO_PERIOD
            if self.cfg['inject_link_stats'] and self.ser_fd is not None:
                dest = self.proto.inject_dest
                ok = self.can_inject_fc() if dest == 'fc' else True
                if ok:
                    self.inject(dest, self.proto.build_info(self.mon))
        # Lueckenfueller-Statuspoll: nur ohne GCS
        if now >= self.next_poll:
            self.next_poll = now + POLL_PERIOD
            if (self.cfg['poll_status'] and self.ser_fd is not None
                    and not self.gcs_present() and self.can_inject_fc()):
                self.to_fc(self.proto.build_poll())


def make_proto(name):
    return MavlinkProtocol() if name == 'mavlink' else MspProtocol()


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH
    cfg = dict(MSP_DEFAULTS)
    try:
        with open(cfg_path) as f:
            cfg.update(json.load(f).get('msp') or {})
    except (OSError, ValueError) as e:
        log(f'config nicht lesbar ({e}), nutze Defaults')
    if cfg['link'] == 'off':
        log('Serial-Bridge deaktiviert (msp.link = off) — schlafe')
        sd_notify('READY=1')
        while True:
            sd_notify('WATCHDOG=1')   # Watchdog auch im Ruhezustand bedienen
            time.sleep(5)
    Bridge(cfg, make_proto(cfg['protocol'])).run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
