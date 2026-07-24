#!/bin/bash
# MAVLink-Smoke-Test: PTY-Paar, armed HEARTBEAT-Quelle, Bridge (protocol=mavlink).
# Prueft: Arm-Erkennung aus HEARTBEAT + RADIO_STATUS-Push zur GCS (nur wenn GCS da).
cd "$(dirname "$0")/.."

cat > /tmp/mav-test-config.json <<'EOF'
{"msp":{"link":"uart","uart_device":"/tmp/fc-a","baud":115200,"udp_port":5762,
"protocol":"mavlink","inject_link_stats":true,"poll_status":true,
"arm_wifi_off":false,"arm_wifi_delay":3}}
EOF

cleanup() {
  [ -n "$LISTPID" ] && kill "$LISTPID" 2>/dev/null
  kill "$BRIDGEPID" "$FCPID" "$SOCATPID" 2>/dev/null
}
trap cleanup EXIT

socat pty,raw,echo=0,link=/tmp/fc-a pty,raw,echo=0,link=/tmp/fc-b >/tmp/socat.log 2>&1 &
SOCATPID=$!
sleep 1
python3 test/fake-fc-mav.py /tmp/fc-b >/tmp/fakefc.log 2>&1 &
FCPID=$!
python3 msp-bridge.py /tmp/mav-test-config.json >/tmp/bridge.log 2>&1 &
BRIDGEPID=$!
sleep 3

# GCS: sendet einen GCS-Heartbeat (macht uns "praesent"), lauscht auf RADIO_STATUS
python3 - 5762 <<'PYEOF' &
import socket, sys, time
port=5762
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(1.0)
GCS_HB=bytes.fromhex('fe0900ffbe000000000006080000032842')
got_radio=0; got_hb=0
end=time.time()+8
while time.time()<end:
    s.sendto(GCS_HB,('127.0.0.1',port))   # regelmaessig praesent bleiben
    try:
        for _ in range(20):
            d,_=s.recvfrom(65535)
            # RADIO_STATUS v1: STX fe, msgid 0x6d(109) an Offset 5
            if d[:1]==b'\xfe' and len(d)>=6 and d[5]==109: got_radio+=1
            if d[:1]==b'\xfe' and len(d)>=6 and d[5]==0: got_hb+=1
    except socket.timeout:
        pass
    time.sleep(0.4)
print(f'GCS: RADIO_STATUS empfangen={got_radio}, HEARTBEATs durchgereicht={got_hb}')
open('/tmp/gcs-result','w').write(f'{got_radio} {got_hb}')
PYEOF
LISTPID=$!
wait $LISTPID
sleep 1

echo "=== bridge.log ==="
cat /tmp/bridge.log
echo "=== Bewertung ==="
grep -E "ARM erkannt|WiFi WUERDE" /tmp/bridge.log || echo "  (keine Arm-Erkennung — FEHLER)"
read RADIO HB < /tmp/gcs-result
echo "RADIO_STATUS an GCS: $RADIO | HEARTBEATs durchgereicht: $HB"

RC=0
grep -q "ARM erkannt" /tmp/bridge.log || RC=1
[ "$RADIO" -gt 0 ] || RC=1
[ "$HB" -gt 0 ] || RC=1
exit $RC
