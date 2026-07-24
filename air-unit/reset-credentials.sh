#!/bin/bash
# UAV-Link: Web-UI- und Hotspot-Passwort auf Default (uavlink2026) zuruecksetzen.
# Nutzt den Reset-Pfad des Fallback-Daemons (SIGUSR2) — identisch zum 10s-GPIO-Hold.
sudo systemctl kill -s USR2 uav-wifi-fallback
echo "Reset ausgeloest: Web-UI + Hotspot -> uavlink2026 (greift in wenigen Sekunden)."
