#!/usr/bin/env python3
"""Fake-FC (MSP) fuer Bridge-Tests an einem PTY.

- Beantwortet MSP-v2-Requests; **spiegelt das Flag-Byte** in der Reply (wie INAV 8+),
  AUSSER NO_REPLY (0x01) war gesetzt -> dann keine Antwort (RC_LINK_STATS/RC_INFO).
- MSP2_INAV_STATUS (0x2000): Reply mit Status-Payload, armingFlags = ARMED wenn --armed.
- v1-Requests: einfache Reply.
- '#' -> CLI-Modus.
- Loggt Funktionscode + Flag jedes Requests.
"""
import os
import struct
import sys
import termios
import time
import tty

MSP2_INAV_STATUS = 0x2000
FLAG_NO_REPLY = 0x01
FLAG_ILMI = 0x02
ARMED = '--armed' in sys.argv


def crc8_dvb_s2(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def msp2(func, payload, flag, dirn=b'>'):
    body = bytes([flag]) + struct.pack('<HH', func, len(payload)) + payload
    return b'$X' + dirn + body + bytes([crc8_dvb_s2(body)])


def mspv1(cmd, payload, dirn=b'>'):
    body = bytes([len(payload), cmd]) + payload
    csum = 0
    for b in body:
        csum ^= b
    return b'$M' + dirn + body + bytes([csum])


def status_payload():
    arming = 0x04 if ARMED else 0x00
    # cycleTime, i2c, sensor, cpu (u16 x4), profile (u8), armingFlags (u32)
    return struct.pack('<HHHHBI', 0, 0, 0, 0, 0, arming)


def main():
    dev = sys.argv[1]
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY)
    tty.setraw(fd)
    buf = bytearray()
    print(f'fake-fc an {dev} (armed={ARMED})', flush=True)
    while True:
        buf += os.read(fd, 4096)
        while buf:
            if buf[0] == ord('#'):
                del buf[:1]
                print('RX raw: CLI enter', flush=True)
                time.sleep(0.05)
                os.write(fd, b'\r\nEntering CLI Mode\r\n# ')
                continue
            if buf[0] != ord('$'):
                del buf[:1]
                continue
            if len(buf) < 3:
                break
            if buf[1] == ord('X'):
                if len(buf) < 8:
                    break
                flag = buf[3]
                func, size = struct.unpack_from('<HH', buf, 4)
                total = 8 + size + 1
                if len(buf) < total:
                    break
                del buf[:total]
                print(f'RX v2 func={func} ({func:#06x}) flag={flag:#04x}', flush=True)
                if flag & FLAG_NO_REPLY:
                    continue                       # keine Antwort (wie INAV)
                payload = status_payload() if func == MSP2_INAV_STATUS else b'ok'
                os.write(fd, msp2(func, payload, flag))   # Flag gespiegelt!
            elif buf[1] == ord('M'):
                if len(buf) < 5:
                    break
                size, cmd = buf[3], buf[4]
                total = 5 + size + 1
                if len(buf) < total:
                    break
                del buf[:total]
                print(f'RX v1 cmd={cmd}', flush=True)
                os.write(fd, mspv1(cmd, b'\x01\x02\x03'))
            else:
                del buf[:1]


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
