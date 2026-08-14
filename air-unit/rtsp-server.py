#!/usr/bin/env python3
"""UAV-Link RTSP-Server.

Zwei Quellenarten, automatisch erkannt (probe_source):

  USB-Dongle (MJPG)   -> SW-JPEG-Decode -> HW-Encode -> RTSP
  HDMI/CSI-Bridge     -> Rohbilder direkt in den HW-Encoder -> RTSP
  (z. B. TC358743)       kein Decode, kein Farbraum-Konverter

Der CSI-Weg spart nicht nur die Analogwandlung davor, sondern auch beide
Zwischenschritte in der Pipeline -- das ist der Latenzgewinn.

Ausgabe: RTSP (UDP-only, zero-latency payloader).
Einstellungen: config.json daneben (per Web-UI aenderbar).
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
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


def run(cmd, timeout=10):
    """Kommando ausfuehren, stdout zurueck. Leerer String bei jedem Fehler."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ''


# v4l2-FourCC -> GStreamer-Formatname, beschraenkt auf das, was die Hardware-
# Encoder OHNE Farbraum-Wandlung fressen: v4l2h264enc UND v4l2jpegenc listen
# beide UYVY/YUY2/NV12/I420 in ihren video/x-raw-Caps (am Geraet nachgesehen,
# nicht angenommen). Fuer eine CSI-Quelle entfaellt damit jeder Konverter --
# kein videoconvert (CPU) und nicht einmal v4l2convert (ISP). Auf einem
# Zero 2 W ist genau das der Unterschied zwischen "laeuft nebenher" und
# "Kern am Anschlag".
RAW_FORMATS = {'UYVY': 'UYVY', 'YUYV': 'YUY2', 'NV12': 'NV12', 'YU12': 'I420'}

# v4l2-Colorspace -> GStreamer-Colorimetry.
#
# Ohne diese Angabe in den Caps fixiert GStreamer die Colorimetry selbst und
# waehlt fuer HD-Aufloesungen bt709. Der TC358743 meldet aber SMPTE 170M
# (= bt601), und v4l2src bricht dann schon vor dem ersten Frame ab:
#   "Device '/dev/video0' does not support 2:3:5:4 colorimetry"
#   "Device wants bt601 colorimetry"
# Am Geraet reproduziert -- mit colorimetry=bt601 laufen 60 Frames 1080p30 in
# 2258 ms durch, ohne bleibt die Pipeline sofort stehen. Gilt nur fuer
# Rohquellen: bei MJPEG steckt der Farbraum im JPEG selbst.
V4L2_COLORIMETRY = {'SMPTE 170M': 'bt601', 'Rec. 709': 'bt709',
                    'sRGB': 'sRGB', 'BT.2020': 'bt2020'}


def raw_colorimetry(dev):
    """Colorimetry der Rohquelle, oder None wenn unbekannt.

    Bewusst nicht auf bt601 hart verdrahtet: ein anderes Capture-Board darf
    etwas anderes melden. Kennen wir den Wert nicht, lassen wir die Angabe weg
    und ueberlassen GStreamer die Wahl wie bisher -- schlechter als vorher wird
    es dadurch nicht.
    """
    m = re.search(r'^\s*Colorspace\s*:\s*(.+?)\s*$',
                  run(['v4l2-ctl', '-d', dev, '--get-fmt-video']), re.M)
    return V4L2_COLORIMETRY.get(m.group(1)) if m else None


def hdmi_setup():
    """EDID + DV-Timings anwenden, falls eine CSI-Bridge da ist. Sonst No-Op."""
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'uav-hdmi-setup')
    if os.path.exists(helper):
        for line in run([helper], timeout=25).splitlines():
            log(line)


def raw_geometry(dev):
    """Aufloesung + Rate einer Rohquelle -- aus dem SIGNAL, nicht aus der Config.

    Eine HDMI-Bridge liefert ausschliesslich das, worauf ihre DV-Timings gelockt
    sind, und lehnt jede andere Geometrie ab. Die Config kann hier also nichts
    vorgeben: wer 720p60 statt 1080p30 will, stellt die HDMI-QUELLE um.
    Ohne gelockte Timings meldet der Treiber 0x0 -- dann ist kein Signal da.
    """
    m = re.search(r'Width/Height\s*:\s*(\d+)/(\d+)',
                  run(['v4l2-ctl', '-d', dev, '--get-fmt-video']))
    if not m or m.group(1) == '0':
        return None
    return {'width': int(m.group(1)), 'height': int(m.group(2)),
            'framerate': dv_framerate(dev)}


def dv_framerate(dev):
    """Bildrate aus den DV-Timings, primaer GERECHNET statt gelesen.

    Pixeltakt und Gesamtgeometrie sind eindeutige Zahlen; die lesbare Rate steht
    nur als Klammerzusatz im Fliesstext ("74250000 Hz (30.00 frames per second)").
    Genau daran bin ich schon einmal gescheitert -- ein Regex auf
    "Frames per second:" fand nie etwas, framerate blieb 0, und der Server hat
    daraufhin JEDE CSI-Quelle als unbrauchbar verworfen. Der Klammerwert bleibt
    als Rueckfall, falls eine v4l-utils-Version die Totale nicht ausgibt.
    """
    out = run(['v4l2-ctl', '-d', dev, '--get-dv-timings'])
    clk = re.search(r'Pixelclock:\s*(\d+)', out)
    tw = re.search(r'Total width:\s*(\d+)', out)
    th = re.search(r'Total height:\s*(\d+)', out)
    if clk and tw and th:
        total = int(tw.group(1)) * int(th.group(1))
        if total:
            return round(int(clk.group(1)) / total)
    m = re.search(r'([\d.]+)\s*frames per second', out, re.I)
    return round(float(m.group(1))) if m else 0


def query_geometry(dev):
    """Geometrie des ERKANNTEN Signals (query), nicht der eingestellten Timings.

    --get-dv-timings zeigt, worauf das Device eingestellt IST -- das bleibt
    auch dann stehen, wenn die Quelle laengst etwas anderes sendet oder weg
    ist. Nur --query-dv-timings fragt den Chip, was JETZT anliegt; ohne Signal
    scheitert der Aufruf (-> None). Bildrate wie in dv_framerate aus
    Pixeltakt/Gesamtgeometrie gerechnet.
    """
    out = run(['v4l2-ctl', '-d', dev, '--query-dv-timings'])
    w = re.search(r'Active width:\s*(\d+)', out)
    h = re.search(r'Active height:\s*(\d+)', out)
    if not w or not h or w.group(1) == '0':
        return None
    clk = re.search(r'Pixelclock:\s*(\d+)', out)
    tw = re.search(r'Total width:\s*(\d+)', out)
    th = re.search(r'Total height:\s*(\d+)', out)
    fps = 0
    if clk and tw and th:
        total = int(tw.group(1)) * int(th.group(1))
        if total:
            fps = round(int(clk.group(1)) / total)
    return {'width': int(w.group(1)), 'height': int(h.group(1)),
            'framerate': fps}


SIGNAL_POLL_S = 3


def watch_signal(src):
    """HDMI-Signal ueberwachen und bei Wechsel/Verlust sauber neu starten.

    Bei einer Rohquelle bestimmt das Signal die Pipeline-Geometrie. Stellt die
    Quelle um (720p60 -> 1080p30) oder faellt sie weg, laeuft die Pipeline ins
    Leere -- ohne dass es jemand merkt, ausser am eingefrorenen Bild. Deshalb:
    alle SIGNAL_POLL_S das erkannte Signal mit dem uebernommenen vergleichen.

    Restart-Mechanik: SIGINT an den eigenen Prozess. Das ist exakt der Weg, den
    auch systemd beim Stop nimmt (KillSignal=SIGINT) -- sauberer Teardown ohne
    SIGKILL am bcm2835-Codec -- und Restart=always startet den Dienst neu, der
    dann in wait_for_source auf das neue Signal lockt.

    Debounce: erst neu starten, wenn ZWEI aufeinanderfolgende Messungen
    denselben abweichenden Wert zeigen. Ein EDID-Hotplug der Gegenseite laesst
    das Signal kurz flackern; darauf sofort zu reagieren wuerde einen
    Restart-Sturm ausloesen.
    """
    expect = (src['width'], src['height'], src['framerate'])
    last = None
    while True:
        time.sleep(SIGNAL_POLL_S)
        g = query_geometry(src['dev'])
        now = (g['width'], g['height'], g['framerate']) if g else None
        # Rate 0 = Pixeltakt (noch) nicht lesbar -> nur Geometrie vergleichen
        if now is not None and (now == expect
                                or (now[2] == 0 and now[:2] == expect[:2])):
            last = None
            continue
        if last is not None and now == last:
            log(f'HDMI-Signal geaendert: {expect[0]}x{expect[1]}@{expect[2]} '
                f'-> {"%dx%d@%d" % now if now else "kein Signal"} '
                f'-- Pipeline wird neu gestartet')
            os.kill(os.getpid(), signal.SIGINT)
            return
        last = now


def probe_source(dev):
    """Was liefert dieses Device? -> dict, oder None wenn unbrauchbar.

    kind='mjpeg' -> fertige JPEGs (USB-Dongle), Geometrie kommt aus der Config.
    kind='raw'   -> unkomprimierte Frames (HDMI/CSI-Bridge), Geometrie aus dem Signal.
    """
    name = sysfs_name(dev)
    if 'bcm2835' in name or 'rpivid' in name:
        return None
    out = run(['v4l2-ctl', '-d', dev, '--list-formats'])
    if 'MJPG' in out:
        return {'kind': 'mjpeg', 'dev': dev, 'name': name}
    for fourcc, gst_fmt in RAW_FORMATS.items():
        if f"'{fourcc}'" in out:
            geom = raw_geometry(dev)
            if not geom or not geom['framerate']:
                return None      # Bridge da, aber (noch) kein Signal
            return dict(kind='raw', dev=dev, name=name, fmt=gst_fmt,
                        colorimetry=raw_colorimetry(dev), **geom)
    return None


def find_source(want=None):
    devs = [want] if want and want != 'auto' else sorted(
        glob.glob('/dev/video*'), key=lambda d: int(re.sub(r'\D', '', d) or 999))
    for dev in devs:
        src = probe_source(dev)
        if src:
            return src
    return None


def wait_for_source(want=None):
    """Auf eine nutzbare Quelle warten.

    Bei CSI reicht "Geraet existiert" nicht: ohne anliegendes HDMI-Signal steht
    die Bridge auf 0x0. Deshalb wird periodisch erneut uav-hdmi-setup angestossen
    -- schaltet die Quelle erst spaeter ein, faengt der Server das ohne Neustart
    auf. Nicht bei jedem Versuch, das Skript schreibt jedes Mal das EDID neu und
    loest damit an der Quelle einen Hotplug aus.
    """
    attempt = 0
    while True:
        if attempt % 8 == 0:
            hdmi_setup()
        src = find_source(want)
        if src:
            geom = (f' {src["width"]}x{src["height"]}@{src["framerate"]}'
                    if src['kind'] == 'raw' else '')
            log(f'Quelle: {src["dev"]} ({src["name"]}) [{src["kind"]}{geom}]')
            return src
        if attempt == 0:
            log('Keine nutzbare Quelle -- USB-Dongle steckt nicht, oder an der '
                'CSI-Bridge liegt kein HDMI-Signal an. Warte...')
        attempt += 1
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


def adopt_geometry(cfg, src):
    """Bei einer Rohquelle setzt das Signal die OBERGRENZE, die Config darf drunter.

    Eine HDMI-Bridge liefert ausschliesslich ihre gelockten Timings -- mehr als
    das Signal geht nie. Eine KLEINERE Zielgeometrie ist dagegen erlaubt: der
    ISP skaliert in Hardware herunter und videorate laesst Frames aus, um
    Bandbreite zu sparen (s. build_pipeline). Wuensche OBERHALB des Signals
    (oder Nullwerte) werden wie bisher auf das Signal gesetzt -- mit Log.
    Das wirkt bewusst auch auf den FPS-Watchdog, der sonst gegen eine Wunschrate
    messen wuerde, die nie kommen kann.
    """
    if src['kind'] != 'raw':
        return
    want = (cfg['width'], cfg['height'], cfg['framerate'])
    have = (src['width'], src['height'], src['framerate'])
    if want == have:
        return
    # Nur die BILDRATE darf unter dem Signal liegen (videorate drop-only).
    # Aufloesungs-Downscale ueber den ISP ist gebaut, aber GEPARKT: der
    # bcm2835-ISP verklemmt sich im RTSP-Harness in einem Race und der erste
    # Frame wird nie fertig (Pipeline haengt stumm, Clients reconnecten
    # endlos) -- Befund und Beweiskette in STATUS.md, 14.08. Deshalb wird die
    # Aufloesung hier immer aufs Signal gesetzt, auch wenn die Config kleiner
    # will (z. B. aus einer alten Config-Datei).
    if ((cfg['width'], cfg['height']) == (src['width'], src['height'])
            and 0 < cfg['framerate'] <= src['framerate']):
        log(f'Signal {have[0]}x{have[1]}@{have[2]}, Ziel @{cfg["framerate"]} '
            f'-- videorate duennt aus (Bandbreite sparen).')
        return
    log(f'Signal liefert {have[0]}x{have[1]}@{have[2]}, Config wollte '
        f'{want[0]}x{want[1]}@{want[2]} -- das Signal gewinnt (nur die '
        f'Bildrate darf reduziert werden, s. Kommentar).')
    cfg['width'], cfg['height'] = have[0], have[1]
    if not 0 < cfg['framerate'] <= src['framerate']:
        cfg['framerate'] = have[2]


def build_pipeline(cfg, src):
    raw = src['kind'] == 'raw'
    # Geometrie kommt aus der Config -- bei einer Rohquelle hat adopt_geometry()
    # sie vorher auf das gesetzt, was das Signal tatsaechlich liefert.
    width, height, fps = cfg['width'], cfg['height'], cfg['framerate']
    bitrate = cfg['bitrate_kbps'] * 1000
    codec = cfg.get('codec')
    if raw and codec == 'mjpeg-src':
        log('MJPEG-Passthrough setzt eine Quelle voraus, die selbst JPEG liefert '
            '-- die CSI-Bridge liefert Rohbilder. Weiche auf H.264 aus.')
        codec = 'h264'
    # Quelle + Decode. Bei CSI faellt der Decode ersatzlos weg: es gibt nichts zu
    # dekodieren, die Frames gehen roh in den Encoder. Das ist neben der
    # wegfallenden Analogwandlung der zweite Gewinn gegenueber dem USB-Weg.
    if raw:
        # Die Quell-Caps beschreiben immer das SIGNAL -- die Bridge liefert
        # nichts anderes. Ist eine kleinere Zielgeometrie konfiguriert
        # (adopt_geometry hat sie durchgelassen), wird dahinter reduziert:
        # videorate drop-only duennt die Bildrate aus (BEVOR skaliert wird,
        # verworfene Frames kosten den ISP dann nichts), der ISP (v4l2convert,
        # /dev/video12) skaliert in Hardware -- keine CPU-Last. PAR 1:1
        # festnageln, damit die minimale Rundung der Zielbreite (16er-Raster)
        # nicht als krummer Pixel-Aspect im Stream landet.
        colorimetry = (f',colorimetry={src["colorimetry"]}'
                       if src.get('colorimetry') else '')
        caps = (f'video/x-raw,format={src["fmt"]},width={src["width"]},'
                f'height={src["height"]},framerate={src["framerate"]}/1'
                f'{colorimetry}')
    else:
        caps = f'image/jpeg,width={width},height={height},framerate={fps}/1'
    source = f'v4l2src device={src["dev"]} ! {caps} '
    decode = '' if raw else '! jpegdec max-errors=-1 '
    scale = ''
    if raw and fps < src['framerate']:
        scale += f'! videorate drop-only=true ! video/x-raw,framerate={fps}/1 '
    # GEPARKT: dieser Zweig ist derzeit unerreichbar (adopt_geometry setzt die
    # Aufloesung immer aufs Signal), weil der bcm2835-ISP im RTSP-Harness in
    # einem Race haengen bleibt -- s. STATUS.md 14.08. Standalone lief exakt
    # diese Kette; der Code bleibt fuer den naechsten Anlauf stehen.
    if raw and (width, height) != (src['width'], src['height']):
        scale += (f'! v4l2convert ! video/x-raw,width={width},height={height},'
                  f'pixel-aspect-ratio=1/1 ')
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
        # Nur an einer MJPEG-Quelle moeglich (oben abgefangen und umgeschaltet).
        return (
            f'( {source}'
            f'! rtpjpegpay name=pay0 pt=26 mtu=1200 )'
        )
    if codec == 'mjpeg':
        # HW-JPEG-Encode. Haelt die Bitrate bei ~10,5 Mbit (Rate-Control, s. o.).
        # Achtung: bei 720p60 reicht das Budget nur fuer 0,19 bit/px -> Artefakte.
        return (
            f'( {source}'
            f'{decode}'
            f'{scale}'
            f'{vrate}'
            f'! v4l2jpegenc '
            f'! rtpjpegpay name=pay0 pt=26 mtu=1200 )'
        )
    cbr = 'video_bitrate_mode=1,' if cfg.get('bitrate_mode') == 'cbr' else ''
    return (
        f'( {source}'
        f'{decode}'
        f'{scale}'
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
    # Immer probeen, auch bei fest konfiguriertem Device: erst die Probe verraet,
    # OB die Quelle JPEG oder Rohbilder liefert -- und danach richtet sich die
    # halbe Pipeline. Ist das konfigurierte Device (noch) nicht nutzbar, warten
    # wir darauf, statt mit einer kaputten Pipeline zu starten.
    src = wait_for_source(cfg.get('device'))
    adopt_geometry(cfg, src)
    if src['kind'] == 'raw':
        threading.Thread(target=watch_signal, args=(src,), daemon=True).start()

    Gst.init(None)
    server = GstRtspServer.RTSPServer()
    server.set_service(str(cfg['port']))
    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_launch(build_pipeline(cfg, src))
    factory.set_shared(True)   # Quelle nur einmal oeffenbar -> Clients teilen Pipeline
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
