#!/bin/bash
# Live-Fuzz: Muellbytes von FC- UND GCS-Seite gegen eine laufende Bridge-Instanz.
# Erfolg = Bridge-PID bleibt 15 s durchgehend gleich (kein Crash/Restart).
cd "$(dirname "$0")/.."

cat > /tmp/fuzz-config.json <<'EOF'
{"msp":{"link":"uart","uart_device":"/tmp/fz-a","baud":115200,"udp_port":5763,
"protocol":"msp","inject_link_stats":true,"poll_status":true}}
EOF

cleanup() { kill $GARBFC $GARBUDP $BRIDGE $SOCAT 2>/dev/null; }
trap cleanup EXIT

socat pty,raw,echo=0,link=/tmp/fz-a pty,raw,echo=0,link=/tmp/fz-b >/dev/null 2>&1 &
SOCAT=$!
sleep 1

# Muell-FC: kontinuierlich Zufallsbytes ins PTY
python3 -c "
import os,random,time
fd=os.open('/tmp/fz-b',os.O_RDWR)
while True:
    os.write(fd,bytes(random.getrandbits(8) for _ in range(random.randint(1,200))))
    time.sleep(0.005)
" >/dev/null 2>&1 &
GARBFC=$!

python3 msp-bridge.py /tmp/fuzz-config.json >/tmp/fuzz-bridge.log 2>&1 &
BRIDGE=$!
sleep 2
PID0=$BRIDGE

# Muell-GCS: Zufalls-Datagramme an den UDP-Port
python3 -c "
import socket,random,time
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
while True:
    s.sendto(bytes(random.getrandbits(8) for _ in range(random.randint(0,400))),('127.0.0.1',5763))
    time.sleep(0.003)
" >/dev/null 2>&1 &
GARBUDP=$!

echo "Bridge-PID: $PID0 — fuzze 15 s (FC-Muell + UDP-Muell)..."
ALIVE=1
for i in $(seq 1 15); do
  sleep 1
  kill -0 "$PID0" 2>/dev/null || { ALIVE=0; echo "  CRASH bei ~${i}s"; break; }
done

echo "=== Ergebnis ==="
if [ "$ALIVE" = 1 ] && kill -0 "$PID0" 2>/dev/null; then
  echo "Bridge lebt nach 15 s Muell durchgehend (PID $PID0 stabil) -> ROBUST"
else
  echo "Bridge gecrasht -> FEHLER"
fi
echo "Abgefangene Loop-Fehler im Log: $(grep -c 'Loop-Fehler' /tmp/fuzz-bridge.log)"
grep 'Loop-Fehler' /tmp/fuzz-bridge.log | head -3
[ "$ALIVE" = 1 ]
