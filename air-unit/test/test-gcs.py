#!/usr/bin/env python3
"""GCS-Testclient (MSP) fuer die Bridge.

Prueft: v2/v1-Passthrough + RTT, dass die GCS-Reply das Flag NICHT gesetzt hat,
und — waehrend einer GCS-Stille — dass KEINE Frames bei der GCS ankommen
(ILMI-Poll-Replies der Bridge werden herausgefiltert). Exit 0 = alles gruen.
"""
import socket
import struct
import sys
import time


def crc8_dvb_s2(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def msp2(func, payload=b'', flag=0, dirn=b'<'):
    body = bytes([flag]) + struct.pack('<HH', func, len(payload)) + payload
    return b'$X' + dirn + body + bytes([crc8_dvb_s2(body)])


def mspv1(cmd, payload=b'', dirn=b'<'):
    body = bytes([len(payload), cmd]) + payload
    csum = 0
    for b in body:
        csum ^= b
    return b'$M' + dirn + body + bytes([csum])


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5761
    addr = ('127.0.0.1', port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    fails = []

    # 1) v2-Passthrough + RTT; Reply-Flag muss 0 sein (nicht ILMI)
    rtts = []
    for i in range(20):
        t0 = time.monotonic()
        s.sendto(msp2(0x1000), addr)
        try:
            data, _ = s.recvfrom(65535)
            if data[:2] == b'$X' and struct.unpack_from('<H', data, 4)[0] == 0x1000:
                rtts.append((time.monotonic() - t0) * 1000)
                if data[3] != 0:
                    fails.append(f'v2 #{i}: Reply-Flag {data[3]:#04x} != 0')
            else:
                fails.append(f'v2 #{i}: unerwartet {data.hex()}')
        except socket.timeout:
            fails.append(f'v2 #{i}: timeout')
        time.sleep(0.02)
    if rtts:
        print(f'v2-Passthrough: {len(rtts)}/20 ok, RTT avg {sum(rtts)/len(rtts):.1f} ms, '
              f'Reply-Flag=0')

    # 2) v1-Passthrough
    s.sendto(mspv1(100), addr)
    try:
        data, _ = s.recvfrom(65535)
        print('v1-Passthrough: ok' if data[:3] == b'$M>' else f'v1 unerwartet {data.hex()}')
        if data[:3] != b'$M>':
            fails.append('v1 falsch')
    except socket.timeout:
        fails.append('v1 timeout')

    # 3) GCS-Stille: Bridge pollt jetzt selbst (ILMI). Wir duerfen NICHTS empfangen.
    print('GCS-Stille 8 s (Bridge pollt INAV_STATUS via ILMI, Replies muessen '
          'gefiltert sein)...')
    leaked = 0
    end = time.monotonic() + 8
    while time.monotonic() < end:
        try:
            data, _ = s.recvfrom(65535)
            leaked += 1
            fails.append(f'LECK: Frame bei GCS waehrend Stille: {data.hex()}')
        except socket.timeout:
            pass
    if leaked == 0:
        print('Stille-Test: ok (keine ILMI-Reply-Leckage zur GCS)')

    # 4) CLI-Passthrough
    s.sendto(b'#', addr)
    try:
        data, _ = s.recvfrom(65535)
        print('CLI-Passthrough: ok' if b'CLI' in data else f'CLI unerwartet {data!r}')
        if b'CLI' not in data:
            fails.append('CLI falsch')
    except socket.timeout:
        fails.append('CLI timeout')

    if fails:
        print('\nFEHLER:')
        for f in fails:
            print(' -', f)
        return 1
    print('\nALLE TESTS GRUEN')
    return 0


if __name__ == '__main__':
    sys.exit(main())
