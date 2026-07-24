#!/bin/bash
# MSP-Integrationstest: PTY-Paar, armed Fake-FC, Bridge (protocol=msp), GCS-Client.
cd "$(dirname "$0")/.."

cat > /tmp/msp-test-config.json <<'EOF'
{"msp":{"link":"uart","uart_device":"/tmp/fc-a","baud":115200,"udp_port":5761,
"protocol":"msp","inject_link_stats":true,"poll_status":true,
"arm_wifi_off":false,"arm_wifi_delay":3}}
EOF

cleanup() {
  [ -n "$GCSPID" ] && kill "$GCSPID" 2>/dev/null
  kill "$BRIDGEPID" "$FCPID" "$SOCATPID" 2>/dev/null
}
trap cleanup EXIT

socat pty,raw,echo=0,link=/tmp/fc-a pty,raw,echo=0,link=/tmp/fc-b >/tmp/socat.log 2>&1 &
SOCATPID=$!
sleep 1
python3 test/fake-fc.py /tmp/fc-b --armed >/tmp/fakefc.log 2>&1 &
FCPID=$!
python3 msp-bridge.py /tmp/msp-test-config.json >/tmp/bridge.log 2>&1 &
BRIDGEPID=$!
sleep 3

python3 test/test-gcs.py 5761
RC=$?
sleep 1

echo "=== bridge.log ==="
cat /tmp/bridge.log
echo "=== ARM-Erkennung + WiFi-Logik ==="
grep -E "ARM erkannt|WiFi WUERDE|WiFi aus" /tmp/bridge.log || echo "  (NICHTS — FEHLER)"
echo "=== fake-fc: ILMI-INAV_STATUS-Requests empfangen? ==="
grep -E "func=8192.*flag=0x02" /tmp/fakefc.log | head -3
echo "   (Anzahl: $(grep -c 'func=8192.*flag=0x02' /tmp/fakefc.log))"
echo "=== fake-fc: NO_REPLY-Pushes (RC_LINK_STATS 0x100d) empfangen, NICHT beantwortet? ==="
echo "   RC_LINK_STATS rx: $(grep -c 'func=4109.*flag=0x01' /tmp/fakefc.log)"

# Bewertung
grep -q "ARM erkannt" /tmp/bridge.log || RC=1
grep -q "WiFi WUERDE aus" /tmp/bridge.log || RC=1
[ "$(grep -c 'func=8192.*flag=0x02' /tmp/fakefc.log)" -gt 0 ] || RC=1
exit $RC
