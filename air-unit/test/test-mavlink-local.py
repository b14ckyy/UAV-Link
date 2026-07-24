#!/usr/bin/env python3
"""Verifiziert den hand-gerollten MAVLink-Codec der Bridge gegen pymavlink.
Laeuft lokal (dev), wo pymavlink installiert ist."""
import importlib.util
import os
import sys

from pymavlink import mavutil
mav = mavutil.mavlink

# msp-bridge.py laden (Bindestrich im Namen)
spec = importlib.util.spec_from_file_location(
    'bridge', os.path.join(os.path.dirname(__file__), 'msp-bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

fails = []


def ref(msgclass_encode, **kw):
    mv = mav.MAVLink(None, srcSystem=kw.pop('_sys'), srcComponent=kw.pop('_comp'))
    mv.seq = kw.pop('_seq')
    msg = msgclass_encode(mv, **kw)
    return msg.pack(mv, force_mavlink1=True)


# 1) CRC16-X25 gegen bekannten Referenz-Frame
ref_rs = ref(mav.MAVLink.radio_status_encode, _sys=51, _comp=68, _seq=1,
             rssi=140, remrssi=0, txbuf=100, noise=20, remnoise=0,
             rxerrors=0, fixed=0)
proto = b.MavlinkProtocol()
proto.seq = 0   # build_injections erhoeht auf 1
import types


class FakeLte:
    rsrp = -101.0   # -> rssi = round((-101+115)*255/30) = round(119) = 119
    snr = 5.0       # -> noise = round(30-5) = 25
    connected = True
    tech = 'lte'
    band = 'B7'
    rx_bps = tx_bps = 0.0


# eigenes RADIO_STATUS bauen und gegen pymavlink-decodierbar pruefen
mine_rs = proto.build_injections(FakeLte, True)[0]
m = mav.MAVLink(None)
m.robust_parsing = True
decoded = None
for byte in mine_rs:
    try:
        msg = m.parse_char(bytes([byte]))
    except Exception:
        msg = None
    if msg:
        decoded = msg
if decoded is None or decoded.get_type() != 'RADIO_STATUS':
    fails.append(f'RADIO_STATUS: pymavlink konnte eigenen Frame nicht dekodieren '
                 f'({mine_rs.hex()})')
else:
    exp_rssi = round((-101 + 115) * 255 / 30)
    exp_noise = round(30 - 5)
    if decoded.rssi != exp_rssi or decoded.noise != exp_noise or decoded.txbuf != 100:
        fails.append(f'RADIO_STATUS Felder falsch: rssi={decoded.rssi} '
                     f'(exp {exp_rssi}), noise={decoded.noise} (exp {exp_noise}), '
                     f'txbuf={decoded.txbuf}')
    else:
        print(f'RADIO_STATUS: pymavlink dekodiert eigenen Frame ok '
              f'(rssi={decoded.rssi}, noise={decoded.noise}, txbuf={decoded.txbuf})')

# 2) HEARTBEAT-Arm-Parsing: pymavlink-Frames durch unseren Sniffer
sniff = b.MavlinkSniffer()
for armed_flag, expect in [(0x80, True), (0x00, False)]:
    mv = mav.MAVLink(None, srcSystem=7, srcComponent=1)
    mv.seq = 0
    hb = mv.heartbeat_encode(type=2, autopilot=3, base_mode=armed_flag,
                             custom_mode=0, system_status=4)
    frame = hb.pack(mv, force_mavlink1=True)
    got = sniff.feed(frame)
    armed = None
    for msgid, sysid, payload in got:
        if msgid == 0:
            armed = bool(payload[6] & 0x80)
            if sysid != 7:
                fails.append(f'HEARTBEAT sysid falsch: {sysid}')
    if armed != expect:
        fails.append(f'HEARTBEAT arm={armed}, erwartet {expect} '
                     f'(frame {frame.hex()}, parsed {got})')
    else:
        print(f'HEARTBEAT base_mode={armed_flag:#04x} -> armed={armed} ok')

# 3) GCS-HEARTBEAT (type=6) darf Arm NICHT setzen
proto2 = b.MavlinkProtocol()
mv = mav.MAVLink(None, srcSystem=255, srcComponent=190)
mv.seq = 0
ghb = mv.heartbeat_encode(type=6, autopilot=8, base_mode=0x80, custom_mode=0,
                          system_status=0)
proto2.process_fc(ghb.pack(mv, force_mavlink1=True))
if proto2.armed is not None:
    fails.append(f'GCS-HEARTBEAT setzte armed={proto2.armed} (sollte None bleiben)')
else:
    print('GCS-HEARTBEAT (type=6) ignoriert -> armed bleibt None ok')

# 4) REQUEST_DATA_STREAM gegen pymavlink dekodieren
proto3 = b.MavlinkProtocol()
proto3.fc_sysid = 7
req = proto3.build_poll()
m2 = mav.MAVLink(None)
m2.robust_parsing = True
dec = None
for byte in req:
    try:
        msg = m2.parse_char(bytes([byte]))
    except Exception:
        msg = None
    if msg:
        dec = msg
if dec is None or dec.get_type() != 'REQUEST_DATA_STREAM':
    fails.append(f'REQUEST_DATA_STREAM: nicht dekodierbar ({req.hex()})')
elif dec.target_system != 7 or dec.req_stream_id != 0 or dec.start_stop != 1:
    fails.append(f'REQUEST_DATA_STREAM Felder falsch: target_sys={dec.target_system} '
                 f'stream_id={dec.req_stream_id} start_stop={dec.start_stop}')
else:
    print(f'REQUEST_DATA_STREAM: dekodiert ok (target_sys={dec.target_system}, '
          f'stream_id={dec.req_stream_id}, rate={dec.req_message_rate})')

if fails:
    print('\nFEHLER:')
    for f in fails:
        print(' -', f)
    sys.exit(1)
print('\nMAVLINK-CODEC: ALLE TESTS GRUEN')
