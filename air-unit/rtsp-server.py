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
# Timeout NICHT unter den GStreamer-Default (60 s) druecken! Eine Session lebt nur weiter,
# solange der Client RTCP-Reports oder RTSP-Keepalives schickt. ffplay, VLC und GStreamer tun
# das -- schlanke RTSP-Apps (z. B. auf Android) oft NICHT. Mit 20 s wurden deren Sessions nach
# ~25 s abgeraeumt: Bild friert ein, Verbindung bricht ab. Genau so beobachtet, in H.264 und
# mit einer unveraenderten Fremd-App, also nicht clientseitig verschuldet.
# Der Zombie-Schutz kommt vom periodischen cleanup(), nicht vom kurzen Timeout: damit lebt
# eine verwaiste Session ~65 s statt ewig. Wer schneller aufraeumen will, setzt in der
# config.json "session_timeout" -- auf eigene Gefahr, s. o.
SESSION_TIMEOUT_S = 60   # = GStreamer-Default; kuerzer killt stille, aber lebende Clients
SESSION_CLEANUP_S = 5    # Pruefintervall -> ein Zombie lebt maximal ~65 s

# FPS-Watchdog (opt-in, s. on_media_configure): Fenster à 2 s. Warmup = 2 Fenster = 4 s,
# damit der ~2 s dauernde Verbindungsaufbau sicher abgeschlossen ist, bevor gemessen wird.
WATCHDOG_WARMUP_WINDOWS = 2
DEFAULTS = {
    'device': 'auto',
    'width': 720,
    'height': 576,
    'framerate': 50,
    'bitrate_kbps': 2000,
    'codec': 'h264',
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


# MJPEG-Messungen vom 26.07. am echten CVBS-Signal.
#
# WICHTIG: Der bcm2835-JPEG-Encoder regelt auf ~10 Mbit/s und ignoriert `compression_quality`
# vollstaendig (Quality 20/50/80/95 -> 21,1/21,5/21,6/21,2 KB/Frame, also identisch).
# Sauber nachgewiesen ohne jeden Decode-Schritt (rohe Frames direkt in den Encoder, also
# der CSI-Fall): gleicher Inhalt, 720p60 -> 20,2 KB/F, 720p30 -> 43,8 KB/F. Halbe Framerate,
# doppelte Framegroesse, Bitrate konstant ~10 Mbit -> Rate-Control, nicht Inhaltsabhaengigkeit.
#
# Es ist ein ZIELWERT, keine harte Decke: sehr einfache Bilder bleiben darunter (7,6 Mbit),
# unkomprimierbares Rauschen ueberschiesst (39,6 Mbit), weil der Encoder am Qualitaetsanschlag
# nicht weiter runterkommt. Der Decode konkurriert NICHT -- ohne Decode dasselbe Verhalten.
# Der HW-Encoder ist der Begrenzer: derselbe dekodierte Inhalt liefert durch den SW-Encoder
# 75,7 KB/F (18,6 Mbit) statt 40,1 KB/F. Fuer CSI-Kameras gilt dasselbe Limit.
#
# Bei festem Budget aendert sich also die Qualitaet pro Frame:
#   720p60 -> 21,8 KB/F = 0,19 bit/px  (sichtbare Blockartefakte)
#   720p30 -> 39,2 KB/F = 0,37 bit/px  (gut)
#   480p60 -> 21,3 KB/F = 0,51 bit/px  (gut)
# Faustregel: unter ~0,25 bit/px wird es haesslich.
MJPEG_HW_MBIT = 10.5

# Passthrough: natives Dongle-JPEG, unkomprimiert weitergereicht (KB/Frame, gemessen).
MJPEG_NATIVE_KB = {(1280, 720): 78.0, (720, 480): 42.0, (720, 576): 44.0}


def mjpeg_estimate_mbit(w, h, fps, passthrough=False):
    """Bitratenschaetzung fuer den MJPEG-Modus (Mbit/s)."""
    if not passthrough:
        return MJPEG_HW_MBIT            # HW-Encoder haelt die Rate konstant
    kb = MJPEG_NATIVE_KB.get((w, h)) or 78.0 * (w * h) / (1280 * 720)
    return kb * 1024 * 8 * fps / 1e6


def mjpeg_bits_per_pixel(w, h, fps):
    """Bildqualitaet des HW-Encoders: Bit pro Pixel bei fester Bitrate."""
    return MJPEG_HW_MBIT * 1e6 / max(fps, 1) / max(w * h, 1)


def build_pipeline(cfg, dev):
    fps = cfg['framerate']
    bitrate = cfg['bitrate_kbps'] * 1000
    codec = cfg.get('codec')
    # Zeitstempel glaetten. Der Dongle liefert auf einem 4-ms-Raster: bei 60 fps kommen
    # Frames abwechselnd 16 und 20 ms auseinander (Soll 16,67) -> +-24 % Abweichung, bei
    # 30 fps nur +-12 %. Player mit fast leerem Puffer erklaeren die 20-ms-Frames fuer zu
    # spaet und verwerfen sie: gemessen 136-221 Drops/Minute bei 60 fps, aber nur 1-4 bei
    # 30 fps -- unabhaengig von Aufloesung und Codec. `videorate` legt die Zeitstempel auf
    # ein exaktes Gitter (gemessen: stdev 1,55 ms -> 0,00 ms) und verwirft dafuer ~1,8 %
    # der Frames.
    #
    # BRINGT IN DER PRAXIS NICHTS -- deshalb standardmaessig AUS ("smooth_pts": true zum
    # Einschalten). Am Client gemessen: weiterhin ~100 Drops/30 s bei 720p60. Der Grund ist
    # logisch: videorate korrigiert nur die ZEITSTEMPEL, die Frames KOMMEN aber weiterhin
    # im 16/20-ms-Rhythmus an. Wer fast ohne Puffer abspielt, verwirft sie trotzdem.
    # Geht ausserdem nur, wo dekodiert wird -- im MJPEG-Passthrough gibt es kein Rohbild.
    vrate = ('! videorate ! video/x-raw,framerate=%d/1 ' % fps
             if cfg.get('smooth_pts', False) else '')
    if codec == 'mjpeg-src':
        # Passthrough: das JPEG des Dongles unveraendert weiterreichen. Volle Quellqualitaet
        # und 0 % CPU, dafuer hohe Bitrate (~35 Mbit bei 720p60). Fuer LAN/WLAN gedacht.
        # Setzt eine Quelle voraus, die selbst MJPEG liefert -- CSI-Kameras koennen das nicht.
        return (
            f'( v4l2src device={dev} '
            f'! image/jpeg,width={cfg["width"]},height={cfg["height"]},framerate={fps}/1 '
            f'! rtpjpegpay name=pay0 pt=26 mtu=1200 )'
        )
    if codec == 'mjpeg':
        # HW-JPEG-Encode. Haelt die Bitrate bei ~10,5 Mbit (Rate-Control, s. o.) und
        # funktioniert mit jeder Quelle, auch mit CSI-Kameras ohne eigenes JPEG.
        # Achtung: bei 720p60 reicht das Budget nur fuer 0,19 bit/px -> Artefakte.
        return (
            f'( v4l2src device={dev} '
            f'! image/jpeg,width={cfg["width"]},height={cfg["height"]},framerate={fps}/1 '
            f'! jpegdec max-errors=-1 '
            f'{vrate}'
            f'! v4l2jpegenc '
            f'! rtpjpegpay name=pay0 pt=26 mtu=1200 )'
        )
    cbr = 'video_bitrate_mode=1,' if cfg.get('bitrate_mode') == 'cbr' else ''
    return (
        f'( v4l2src device={dev} '
        f'! image/jpeg,width={cfg["width"]},height={cfg["height"]},framerate={fps}/1 '
        f'! jpegdec max-errors=-1 '
        f'{vrate}'
        f'! v4l2h264enc extra-controls="controls,'
        f'video_bitrate={bitrate},{cbr}'
        f'h264_i_frame_period={fps},repeat_sequence_header=1" '
        f'! video/x-h264,level=(string)4 '
        f'! h264parse '
        f'! rtph264pay name=pay0 pt=96 config-interval=1 '
        f'aggregate-mode=zero-latency mtu=1200 )'
    )


def on_media_configure(factory, media, cfg):
    """FPS-Watchdog -- STANDARDMAESSIG AUS (config: "fps_watchdog": true).

    Er beendet die Media per EOS, wenn am Payloader zu wenige Frames ankommen.
    Weil die Media geteilt ist (set_shared), trifft dieses EOS ALLE Clients
    gleichzeitig -- ein kurzer Aussetzer wirft also jeden Zuschauer raus, statt
    ihn nur ruckeln zu lassen. Gemessen: 57 Ausloesungen in 3 Stunden, was
    genau das Bild aus 'verbindet nicht / Freeze / Reconnect' erzeugt.

    Gebaut wurde er gegen angeblich einbrechende Framerates des USB-Dongles.
    Diese Annahme ist NICHT belegt: das Verhalten liess sich auf keinem anderen
    System reproduzieren (Pi 3, Pi 5, Debian-Laptop, Windows, OTG direkt, sowie
    mit einem zweiten Dongle eines anderen Herstellers). Solange die Ursache
    nicht sauber verifiziert ist, darf diese Notbremse nicht per Default in
    einen laufenden Stream eingreifen."""
    if not cfg.get('fps_watchdog'):
        return
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
        if state['windows'] <= WATCHDOG_WARMUP_WINDOWS:
            return True   # Warmup ignorieren: ein Connect braucht gut 2 s, ein
                          # zu kurzes Warmup misst die noch leere Pipeline und
                          # killt die Session genau waehrend des Verbindens
        if n < target:    # unter 50% der Sollrate (Fenster = 2s)
            log(f'FPS-WATCHDOG: ~{n/2:.0f} fps (Soll {target}) -> Session-Neustart (EOS)')
            element.send_event(Gst.Event.new_eos())
            return False
        return True

    GLib.timeout_add(2000, check)


def install_session_reaper(server, cfg):
    """Kurzes Session-Timeout + periodisches Aufraeumen (s. Kommentar oben).
    Muss VOR attach() passieren, damit kein Client die Signalbindung verpasst."""
    timeout = int(cfg.get('session_timeout', SESSION_TIMEOUT_S) or SESSION_TIMEOUT_S)

    def on_new_session(client, session):
        session.set_timeout(timeout)

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
    log(f'Session-Reaper aktiv: Timeout {timeout}s, '
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
    install_session_reaper(server, cfg)
    server.attach(None)
    log(f'RTSP-Server laeuft: rtsp://0.0.0.0:{cfg["port"]}{cfg["mount"]} (UDP-only)')

    loop = GLib.MainLoop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *a: (loop.quit(), False)[1])
    loop.run()
    log('Beende...')


if __name__ == '__main__':
    sys.exit(main())
