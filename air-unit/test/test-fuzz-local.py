#!/usr/bin/env python3
"""Fuzz: zufaellige/boesartige Bytes durch beide Protokoll-Parser + Builder.
Darf NIE eine Exception werfen. Laeuft lokal (kein termios noetig)."""
import importlib.util
import os
import random
import struct
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_bridge = os.path.join(_here, 'msp-bridge.py')
if not os.path.exists(_bridge):
    _bridge = os.path.join(os.path.dirname(_here), 'msp-bridge.py')
spec = importlib.util.spec_from_file_location('bridge', _bridge)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

random.seed(1)
fails = []


class FuzzLte:
    def __init__(self):
        # Extremwerte/None gemischt
        self.rsrp = random.choice([None, -140.0, -40.0, 0.0, -1e9, 1e9])
        self.snr = random.choice([None, -100.0, 100.0, 0.0, 1e9])
        self.tech = random.choice(['lte', '', 'xyz', None, '5gnr'])
        self.band = random.choice(['B7', '', 'X'*99, None])
        self.rx_bps = random.choice([0.0, 1e12, -5.0])
        self.tx_bps = random.choice([0.0, 1e12, -5.0])
        self.connected = random.choice([True, False])


# 1) Parser-Fuzz: zufaellige Byte-Bloecke, auch mit MSP/MAVLink-Praeambeln
def rand_chunk():
    n = random.randint(0, 300)
    data = bytearray(random.getrandbits(8) for _ in range(n))
    # gelegentlich echte Praeambeln einstreuen, um Parser-Pfade zu triggern
    for pre in (b'$X<', b'$M<', b'$X>', b'\xfe', b'\xfd', b'$'):
        if random.random() < 0.3:
            pos = random.randint(0, max(0, len(data)))
            data[pos:pos] = pre
    return bytes(data)


for proto_cls in (b.MspProtocol, b.MavlinkProtocol):
    for _ in range(20000):
        p = proto_cls()
        try:
            for _ in range(random.randint(1, 4)):
                p.process_fc(rand_chunk())
                p.process_gcs(rand_chunk())
                p.fc_at_boundary(); p.gcs_at_boundary()
                p.fc_last_feed(); p.cli_suspected()
                if random.random() < 0.2:
                    p.flush_fc()
        except Exception as e:
            fails.append(f'{proto_cls.__name__}.process*: {e!r}')
            break
print(f'Parser-Fuzz: {proto_cls.__name__} und MSP je 20000 Runden durch')

# 2) Builder-Fuzz: Extrem-LTE-Werte durch alle Message-Builder
for proto_cls in (b.MspProtocol, b.MavlinkProtocol):
    for _ in range(5000):
        p = proto_cls()
        if proto_cls is b.MavlinkProtocol:
            p.fc_sysid = random.choice([None, 0, 255, 7])
        lte = FuzzLte()
        try:
            for m in p.build_injections(lte, random.choice([True, False])):
                pass
            p.build_info(lte)
            p.build_poll()
        except Exception as e:
            fails.append(f'{proto_cls.__name__}.build*: {e!r} (lte '
                         f'rsrp={lte.rsrp} snr={lte.snr} tech={lte.tech} band={lte.band})')
            break
print('Builder-Fuzz: MSP + MAVLink je 5000 Runden mit Extremwerten durch')

# 3) MSP _read_status mit truncated/random INAV_STATUS-Frames
mp = b.MspProtocol()
for _ in range(20000):
    func = random.choice([0x2000, 150, 0x1000, random.randint(0, 65535)])
    plen = random.randint(0, 40)
    payload = bytes(random.getrandbits(8) for _ in range(plen))
    body = bytes([random.getrandbits(8)]) + struct.pack('<HH', func, len(payload)) + payload
    frame = b'$X>' + body + bytes([b.crc8_dvb_s2(body)])
    try:
        mp._read_status(func, frame)
    except Exception as e:
        fails.append(f'_read_status: {e!r}')
        break
print('_read_status-Fuzz: 20000 truncated INAV_STATUS-Frames durch')

if fails:
    print('\nFEHLER:')
    for f in fails:
        print(' -', f)
    sys.exit(1)
print('\nFUZZ: ALLE PARSER/BUILDER ROBUST (keine Exception)')
