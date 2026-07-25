#!/usr/bin/env python3
"""UAV-Link RTSP-Server.

Erstes brauchbares Capture-Device (MJPG, kein bcm2835-Codec) -> SW-JPEG-Decode
-> HW-H.264-Encode (CBR) -> RTSP (UDP-only, zero-latency payloader).
Einstellungen: config.json daneben (spaeter per Web-UI aenderbar).
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
gi.require_version('GstRtsp', '1.0')
from gi.repository import Gst, GstRtsp, GstRtspServer, GLib

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

# Session-Reaper: gst-rtsp-server raeumt abgelaufene Sessions NICHT von selbst auf --
# ohne periodisches session_pool.cleanup() leben sie ewig weiter. Ein Client, der ohne
# TEARDOWN verschwindet (Funkloch, gekillter Prozess, Reconnect-Loop), hinterlaesst also
# eine Zombie-Session, deren udpsink weiter mit voller Bitrate an einen toten Port sendet.
# Das summiert sich: Last steigt -> FPS fallen -> der FPS-Watchdog beendet die GETEILTE
# Media fuer ALLE Clients -> alle reconnecten -> naechste Runde. Genau diese Spirale.
# Clients ohne TEARDOWN sind im Funkbetrieb der Normalfall, nicht der Fehlerfall.
SESSION_TIMEOUT_S = 20   # ohne RTCP/Keepalive gilt eine Session als tot (Default: 60 s)
SESSION_CLEANUP_S = 5    # Pruefintervall -> ein Zombie lebt maximal ~25 s
DEFAULTS = {
    'device': 'auto',
    'width': 720,
    'height': 576,
    'framerate': 50,
    'bitrate_kbps': 2000,
    'port': 8554,
    'mount': '/cam',
}


def log(msg):
    print(msg, flush=True)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError) as e:
        log(f'config.json nicht lesbar ({e}), nutze Defaults')
    return cfg


def sysfs_name(dev):
    try:
        with open(f'/sys/class/video4linux/{os.path.basename(dev)}/name') as f:
            return f.read().strip()
    except OSError:
        return ''


def is_capture_candidate(dev):
    name = sysfs_name(dev)
    if 'bcm2835' in name or 'rpivid' in name:
        return False
    try:
        out = subprocess.run(['v4l2-ctl', '-d', dev, '--list-formats'],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return 'MJPG' in out


def find_device():
    devs = sorted(glob.glob('/dev/video*'),
                  key=lambda d: int(re.sub(r'\D', '', d) or 999))
    for dev in devs:
        if is_capture_candidate(dev):
            return dev
    return None


def wait_for_device():
    while True:
        dev = find_device()
        if dev:
            log(f'Capture-Device gefunden: {dev} ({sysfs_name(dev)})')
            return dev
        log('Kein Capture-Device gefunden, warte...')
        time.sleep(2)


def build_pipeline(cfg, dev):
    fps = cfg['framerate']
    bitrate = cfg['bitrate_kbps'] * 1000
    cbr = 'video_bitrate_mode=1,' if cfg.get('bitrate_mode') == 'cbr' else ''
    return (
        f'( v4l2src device={dev} '
        f'! image/jpeg,width={cfg["width"]},height={cfg["height"]},framerate={fps}/1 '
        f'! jpegdec max-errors=-1 '
        f'! v4l2h264enc extra-controls="controls,'
        f'video_bitrate={bitrate},{cbr}'
        f'h264_i_frame_period={fps},repeat_sequence_header=1" '
        f'! video/x-h264,level=(string)4 '
        f'! h264parse '
        f'! rtph264pay name=pay0 pt=96 config-interval=1 '
        f'aggregate-mode=zero-latency mtu=1200 )'
    )


def on_media_configure(factory, media, cfg):
    """FPS-Watchdog: Kriech-Sessions (Dongle-Lotterie) erkennen und Media
    absichtlich beenden -> Client-Reconnect wuerfelt eine neue Session."""
    element = media.get_element()
    pay = element.get_by_name('pay0')
    if pay is None:
        return
    state = {'count': 0, 'windows': 0, 'alive': True}

    def probe_cb(pad, info):
        state['count'] += 1
        return Gst.PadProbeReturn.OK

    # WICHTIG: sink-Pad, nicht src! Am src verschickt rtph264pay Buffer-LISTEN,
    # die ein BUFFER-Probe nicht sieht -> Watchdog wuerde gesunde Sessions killen.
    # Am sink kommt genau 1 Buffer pro Videoframe an.
    pay.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, probe_cb)
    media.connect('unprepared', lambda m: state.update(alive=False))
    target = cfg['framerate']

    def check():
        if not state['alive']:
            return False
        n, state['count'] = state['count'], 0
        state['windows'] += 1
        if state['windows'] <= 1:
            return True   # Warmup-Fenster ignorieren
        if n < target:    # unter 50% der Sollrate (Fenster = 2s)
            log(f'FPS-WATCHDOG: ~{n/2:.0f} fps (Soll {target}) -> Session-Neustart (EOS)')
            element.send_event(Gst.Event.new_eos())
            return False
        return True

    GLib.timeout_add(2000, check)


def install_session_reaper(server):
    """Kurzes Session-Timeout + periodisches Aufraeumen (s. Kommentar oben).
    Muss VOR attach() passieren, damit kein Client die Signalbindung verpasst."""
    def on_new_session(client, session):
        session.set_timeout(SESSION_TIMEOUT_S)

    server.connect('client-connected',
                   lambda srv, client: client.connect('new-session', on_new_session))

    pool = server.get_session_pool()

    def reap():
        n = pool.cleanup()
        if n:
            log(f'SESSION-REAPER: {n} abgelaufene Session(s) entfernt '
                f'(Client weg ohne TEARDOWN)')
        return True   # Timer weiterlaufen lassen

    GLib.timeout_add_seconds(SESSION_CLEANUP_S, reap)
    log(f'Session-Reaper aktiv: Timeout {SESSION_TIMEOUT_S}s, '
        f'Cleanup alle {SESSION_CLEANUP_S}s')


def main():
    cfg = load_config()
    dev = cfg['device']
    if dev == 'auto':
        dev = wait_for_device()
    else:
        log(f'Nutze konfiguriertes Device: {dev}')

    Gst.init(None)
    server = GstRtspServer.RTSPServer()
    server.set_service(str(cfg['port']))
    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_launch(build_pipeline(cfg, dev))
    factory.set_shared(True)   # Dongle nur einmal oeffenbar -> Clients teilen Pipeline
    factory.set_latency(0)
    factory.set_protocols(GstRtsp.RTSPLowerTrans.UDP)  # kein TCP-Fallback, kein Resend
    factory.connect('media-configure', on_media_configure, cfg)
    server.get_mount_points().add_factory(cfg['mount'], factory)
    install_session_reaper(server)
    server.attach(None)
    log(f'RTSP-Server laeuft: rtsp://0.0.0.0:{cfg["port"]}{cfg["mount"]} (UDP-only)')

    loop = GLib.MainLoop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *a: (loop.quit(), False)[1])
    loop.run()
    log('Beende...')


if __name__ == '__main__':
    sys.exit(main())
