#!/usr/bin/env python3
"""Fake-FC (MAVLink) fuer Bridge-Tests: spuckt periodisch armed HEARTBEATs an ein PTY.
Frames sind pymavlink-generiert (v1) und fest verdrahtet."""
import os
import sys
import termios
import time
import tty

FC_ARMED = bytes.fromhex('fe09000701000000000002038004035024')   # sys=7, base_mode=0x80


def main():
    dev = sys.argv[1]
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY)
    tty.setraw(fd)
    print(f'fake-fc-mav an {dev} (armed HEARTBEAT 2 Hz)', flush=True)
    while True:
        try:
            os.write(fd, FC_ARMED)
        except OSError:
            pass
        # eingehende Bytes (z. B. REQUEST_DATA_STREAM) nur wegschlucken
        time.sleep(0.5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
