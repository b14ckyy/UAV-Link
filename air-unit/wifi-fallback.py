#!/usr/bin/env python3
"""UAV-Link WiFi-Fallback: Access Point, wenn kein bekanntes WLAN da ist.

Regeln:
- Radio AN, aber 60 s mit keinem WLAN verbunden -> Hotspot 'uav-hotspot' starten
  (Ersteinrichtung/Feld). VPN ueber LTE bleibt unberuehrt.
- Radio AUS (bewusster LTE-Test via Web-UI) -> nichts tun.
- Hotspot aktiv -> bleibt, bis er per Web-UI/Toggle beendet wird.
- Taster an GPIO21 (Pin 40, interner Pullup) gegen GND (Pin 39):
  3 s halten -> Hotspot sofort (SIGUSR1 simuliert das).
  10 s halten -> Credential-Reset: Web-UI- + Hotspot-Passwort auf Default (SIGUSR2 simuliert das).
  (GPIO3/Pin 5 bewusst frei gelassen: I2C fuer spaeteres OLED-Statusdisplay.)
"""
import os
import signal
import subprocess
import threading
import time

HOTSPOT = 'uav-hotspot'
HOTSPOT_SSID = 'UAV-Link'
HOTSPOT_PSK = 'uavlink2026'
# gleiches Verzeichnis wie webui.py (kein hardcodierter Username/Home)
AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'webui-auth.json')
TIMEOUT_S = 60
POLL_S = 5
BUTTON_GPIO = 21
HOLD_S = 3
RESET_HOLD_S = 10

force_ap = False
force_reset = False


def log(msg):
    print(msg, flush=True)


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ''


def ensure_profile():
    """Hotspot-Profil idempotent anlegen (autoconnect aus!)."""
    names = [l.rpartition(':')[0] for l in
             sh(['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show']).splitlines()]
    if HOTSPOT in names:
        return
    log(f'Lege Hotspot-Profil {HOTSPOT} an (SSID {HOTSPOT_SSID})')
    sh(['nmcli', 'connection', 'add', 'type', 'wifi', 'ifname', 'wlan0',
        'con-name', HOTSPOT, 'autoconnect', 'no', 'ssid', HOTSPOT_SSID,
        '802-11-wireless.mode', 'ap', '802-11-wireless.band', 'bg',
        'ipv4.method', 'shared', 'ipv6.method', 'disabled',
        'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', HOTSPOT_PSK])


def radio_on():
    return sh(['nmcli', 'radio', 'wifi']) == 'enabled'


def wlan_state():
    """'ap' | 'connected' | 'disconnected' (aus NM-Sicht fuer wlan0)."""
    for line in sh(['nmcli', '-t', '-f', 'DEVICE,STATE,CONNECTION',
                    'device']).splitlines():
        parts = line.split(':')
        if parts[0] == 'wlan0' and len(parts) >= 3:
            if parts[1] == 'connected':
                return 'ap' if parts[2] == HOTSPOT else 'connected'
            return 'disconnected'
    return 'disconnected'


def start_ap(reason):
    log(f'Starte Access Point ({reason})')
    if not radio_on():
        sh(['nmcli', 'radio', 'wifi', 'on'])
        time.sleep(2)
    out = sh(['nmcli', 'connection', 'up', HOTSPOT], timeout=60)
    log(out or 'nmcli up: keine Ausgabe')


def on_button(*_):
    global force_ap
    force_ap = True


def on_reset(*_):
    global force_reset
    force_reset = True


def reset_credentials():
    """Web-UI- und Hotspot-Passwort auf Default zuruecksetzen.
    Web-UI: Auth-Datei loeschen -> webui heilt zu Default. Hotspot: PSK setzen."""
    log('CREDENTIAL-RESET: Web-UI- + Hotspot-Passwort -> Default')
    try:
        os.remove(AUTH_FILE)
    except OSError:
        pass
    sh(['nmcli', 'connection', 'modify', HOTSPOT, 'wifi-sec.psk', HOTSPOT_PSK])


def setup_button():
    try:
        from gpiozero import Button
        btn = Button(BUTTON_GPIO, pull_up=True, hold_time=HOLD_S)
        btn.when_held = on_button                    # 3 s -> AP
        state = {'timer': None}

        def pressed():
            state['timer'] = threading.Timer(RESET_HOLD_S, on_reset)  # 10 s -> Reset
            state['timer'].daemon = True
            state['timer'].start()

        def released():
            if state['timer']:
                state['timer'].cancel()

        btn.when_pressed = pressed
        btn.when_released = released
        btn._uav_state = state   # Referenz halten (GC)
        log(f'GPIO-Button aktiv: GPIO{BUTTON_GPIO} — {HOLD_S}s Hold -> AP, '
            f'{RESET_HOLD_S}s -> Passwort-Reset')
        return btn   # Referenz halten, sonst raeumt der GC den Button ab
    except Exception as e:
        log(f'GPIO-Button nicht verfuegbar ({e}) — nur SIGUSR1/SIGUSR2')
        return None


def main():
    global force_ap, force_reset
    signal.signal(signal.SIGUSR1, on_button)
    signal.signal(signal.SIGUSR2, on_reset)
    ensure_profile()
    button = setup_button()  # noqa: F841
    unconnected = 0
    log(f'WiFi-Fallback laeuft: {TIMEOUT_S} s ohne WLAN -> AP {HOTSPOT_SSID}')
    while True:
        if force_reset:
            force_reset = False
            reset_credentials()
        if force_ap:
            force_ap = False
            if wlan_state() != 'ap':
                start_ap('Button/SIGUSR1')
            unconnected = 0
        if not radio_on():
            unconnected = 0          # bewusst aus -> kein Fallback
        else:
            state = wlan_state()
            if state == 'disconnected':
                unconnected += POLL_S
                if unconnected >= TIMEOUT_S:
                    start_ap(f'{TIMEOUT_S} s ohne bekanntes WLAN')
                    unconnected = 0
            else:
                unconnected = 0
        time.sleep(POLL_S)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
