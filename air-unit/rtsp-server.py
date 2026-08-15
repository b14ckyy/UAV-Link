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
import struct
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
        # Rate 0 = Pixeltakt (noch) nicht lesbar -> nur Geometrie vergleichen.
        # Rate +-1 Hz tolerieren: die Rate ist aus dem Pixeltakt GERUNDET --
        # eine krumme Quelle (Laptop liefert z.B. 800x600@60,75) pendelt
        # sonst um die Rundungsgrenze und loest im Minutentakt grundlose
        # Restarts aus (gemessen 15.08.: 60<->61-Flapping alle ~20 s).
        if now is not None and now[:2] == expect[:2] and (
                now[2] == 0 or abs(now[2] - expect[2]) <= 1):
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


def build_pipelines(cfg, src):
    """(Capture-Launch, Media-Launch, is_h264) -- getrennte Pipelines.

    Capture (Quelle -> Encoder -> appsink) laeuft PERSISTENT im Prozess;
    die RTSP-Media bekommt fertige Frames per appsrc. Hintergrund s.
    StreamHub-Docstring: Client-Joins/-Teardowns fahren die geteilte
    Media durch PAUSED und wuerden sonst die Kamera stoppen.
    """
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
    # OSD-Burn-in: nach videorate/Scale (weniger Frames zu stanzen), direkt
    # vor dem Encoder. Nur wenn die Engine aktiv ist (Rohquelle oder
    # dekodiertes MJPEG, Font da; im Passthrough gibt es kein Rohbild).
    # Die queue dahinter entkoppelt Stanzen und Encoder in eigene Threads
    # (sonst serialisiert der Streaming-Thread beides: gemessen kostete
    # jede Stanz-Millisekunde ~3 fps). max-size-buffers=2 haelt die
    # Zusatzlatenz bei maximal einem Frame.
    osd = '! osdstamp ! queue max-size-buffers=2 ' if OSD_ENGINE else ''
    if OSD_ENGINE and not raw:
        # MJPEG-Pfad: zusaetzlich eine queue VOR dem Stanzer. Der SW-JPEG-
        # Decode laeuft sonst mit dem Stanzen im selben Thread -- bei
        # 720p30 sattelte der auf 85 % eines Kerns und beides zusammen riss
        # die 30 fps (gemessen 27,8 fps, stille appsink-Drops als sporadische
        # Mikro-Freezes beim Client). So bekommt jedes seinen eigenen Kern.
        osd = '! queue max-size-buffers=2 ' + osd
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
    sink = ('! appsink name=vidsink emit-signals=true sync=false '
            'max-buffers=4 drop=true')
    jpeg_caps = f'image/jpeg,width={width},height={height},framerate={fps}/1'
    asrc = ('appsrc name=vidsrc is-live=true format=time do-timestamp=true '
            'max-buffers=60 leaky-type=downstream ')
    if codec == 'mjpeg-src':
        # Passthrough: das JPEG des Dongles unveraendert weiterreichen. Volle Quellqualitaet
        # und 0 % CPU, dafuer hohe Bitrate (~35 Mbit bei 720p60). Fuer LAN/WLAN gedacht.
        # Nur an einer MJPEG-Quelle moeglich (oben abgefangen und umgeschaltet).
        return (
            f'{source}{sink}',
            f'( {asrc}caps="{jpeg_caps}" ! rtpjpegpay name=pay0 pt=26 mtu=1200 )',
            False,
        )
    if codec == 'mjpeg':
        # HW-JPEG-Encode. Haelt die Bitrate bei ~10,5 Mbit (Rate-Control, s. o.).
        # Achtung: bei 720p60 reicht das Budget nur fuer 0,19 bit/px -> Artefakte.
        return (
            f'{source}{decode}{scale}{vrate}{osd}! v4l2jpegenc {sink}',
            f'( {asrc}caps="{jpeg_caps}" ! rtpjpegpay name=pay0 pt=26 mtu=1200 )',
            False,
        )
    cbr = 'video_bitrate_mode=1,' if cfg.get('bitrate_mode') == 'cbr' else ''
    # h264parse config-interval=-1 + byte-stream: SPS/PPS inline vor JEDEM
    # IDR -- so ist jedes gecachte Keyframe (Preview!) und jeder Media-
    # Einstieg selbsttragend dekodierbar.
    h264_caps = 'video/x-h264,stream-format=byte-stream,alignment=au'
    return (
        (
            f'{source}'
            f'{decode}'
            f'{scale}'
            f'{vrate}'
            f'{osd}'
            f'! v4l2h264enc extra-controls="controls,'
            f'video_bitrate={bitrate},{cbr}'
            f'h264_i_frame_period={fps},repeat_sequence_header=1" '
            f'! video/x-h264,level=(string)4 '
            f'! h264parse config-interval=-1 '
            f'! {h264_caps} '
            f'{sink}'
        ),
        (
            f'( {asrc}caps="{h264_caps}" '
            f'! h264parse '
            f'! rtph264pay name=pay0 pt=96 config-interval=1 '
            f'aggregate-mode=zero-latency mtu=1200 )'
        ),
        True,
    )


# ============================== FPV-OSD (Burn-in) =============================
# MSP-DisplayPort-OSD, eingebrannt vor dem Encoder. Arbeitsteilung:
#   uav-osd.py   liest den DisplayPort-UART und schickt bei jedem "draw" das
#                Zeichen-Grid als UDP-Paket an OSD_UDP_PORT (localhost).
#   OsdEngine    laedt das Font-PNG (SneakyFPV-Format: 2 Spalten x 256 Zeilen
#                Glyphen, beliebige Variante -- wird per GdkPixbuf auf die
#                Zellgroesse der aktuellen Aufloesung skaliert), kompiliert
#                pro Glyphe UYVY-Byte-Laeufe vor und baut bei jedem Grid-
#                Update (~10 Hz) die flache Lauftabelle.
#   osdstamp     (GstBase-Element) ruft pro Frame einmal libosdstamp.so:
#                memcpy der Laeufe in den Framebuffer. Gemessen 15.08.:
#                0,83 ms/Frame fuer ein 162-Zellen-OSD, 720p60 gehalten.
# Frame-LESEN nur fuer die Saumbytes (DMA-Puffer sind uncached, ~125 MB/s
# lesend -- s. STATUS 15.08.; nach jpegdec liegt dagegen gecachter System-
# speicher an, dort ist das Blending fast gratis).
# Unterstuetzte Formate: UYVY (CSI/Dongle roh, interleavte Laeufe) sowie
# I420/Y42B (MJPEG nach jpegdec bzw. YU12-Quellen): drei Planes, je eigene
# Laeufe -- dieselben C-Funktionen, die dst-Offsets zeigen in die Plane.
OSD_UDP_PORT = 5761
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'fonts', 'osd-font.png')
OSD_ENGINE = None


class OsdEngine:
    """Font -> Glyphen-Laeufe -> Lauftabelle; UDP-Listener; Stanz-Element."""

    @staticmethod
    def create(cfg, src):
        """Baut die Engine oder gibt None zurueck -- niemals den Stream reissen."""
        osd = cfg.get('osd') or {}
        if not (osd.get('enabled') and osd.get('mode', 'burnin') == 'burnin'):
            return None
        if src['kind'] == 'raw':
            fmt = src.get('fmt')
            if fmt not in ('UYVY', 'I420'):
                log(f'OSD-Burn-in: Rohformat {fmt} nicht unterstuetzt -- '
                    f'uebersprungen')
                return None
        elif src['kind'] == 'mjpeg':
            if cfg.get('codec') == 'mjpeg-src':
                log('OSD-Burn-in: MJPEG-Passthrough hat kein Rohbild -- '
                    'uebersprungen')
                return None
            # jpegdec liefert planar: I420 bei 4:2:0-JPEGs, Y42B bei 4:2:2.
            # Annahme I420; weicht die Aushandlung ab, zieht set_caps nach.
            fmt = 'I420'
        else:
            log('OSD-Burn-in: Quellart unbekannt -- uebersprungen')
            return None
        if not os.path.exists(FONT_PATH):
            log('OSD-Burn-in: kein Font hochgeladen -- uebersprungen')
            return None
        try:
            eng = OsdEngine(cfg['width'], cfg['height'], fmt)
        except Exception as e:            # noqa: BLE001 -- Stream schuetzen
            log(f'OSD-Burn-in deaktiviert: {e}')
            return None
        log('OSD-Burn-in aktiv: Zellgroesse adaptiv, wartet auf erstes '
            'FC-Grid')
        return eng

    def __init__(self, width, height, fmt='UYVY'):
        import ctypes
        import numpy as np
        self.np = np
        lib_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'libosdstamp.so')
        self.lib = ctypes.CDLL(lib_path)
        self.lib.stamp.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_uint32]
        self.lib.blend.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_uint32]
        self.ctypes = ctypes
        self._font_cell = None        # Zellgroesse, fuer die das PNG geladen ist
        self._layout = None           # (fmt, W, H, offsets, strides)
        self._meta_key = None         # zuletzt gesehene GstVideoMeta-Geometrie
        self.grid_dims = None         # (rows, cols) des FC-Grids -- adaptiv
        self.cell_w = self.cell_h = 0
        self.glyphs = []
        # Kompilieren (Listener- ODER Streaming-Thread) gegeneinander
        # serialisieren; stamp() bleibt lock-frei ueber Tabellen-Snapshots.
        self._cl = threading.Lock()
        self.tables = None            # opak:  (runs_ptr, n_runs, runs_ref)
        self.btables = None           # Saum: dito, Pools bval/balf parallel
        self._last_grid = None
        # Timing-Diagnose (UAV_OSD_TIMING=1 in der Service-Umgebung)
        self._timing = bool(os.environ.get('UAV_OSD_TIMING'))
        self._tsum = self._tmax = 0.0
        self._tn = 0
        self.set_layout(fmt, width, height)
        self._start_listener()

    # --- Bildlayout: Format/Geometrie -> Planes -------------------------------
    def set_layout(self, fmt, W, H, offsets=None, strides=None):
        """Planes als (byte_offset, stride, hshift, vshift, bytes/px).

        UYVY ist eine interleavte "Plane"; bei I420/Y42B bekommen Y/U/V
        eigene Laeufe. offsets/strides kommen aus GstVideoMeta, wenn der
        Puffer gepolstert ist -- sonst dichtgepackte Annahme.
        """
        key = (fmt, W, H, tuple(offsets or ()), tuple(strides or ()))
        if key == self._layout:
            return
        if fmt == 'UYVY':
            s0 = strides[0] if strides else W * 2
            planes = [((offsets or (0,))[0], s0, 0, 0, 2)]
        elif fmt in ('I420', 'Y42B'):
            vs = 1 if fmt == 'I420' else 0    # Chroma vertikal halbiert?
            sy, sc = (strides[0], strides[1]) if strides else (W, W // 2)
            off = offsets or (0, sy * H, sy * H + sc * (H >> vs))
            planes = [(off[0], sy, 0, 0, 1),
                      (off[1], sc, 1, vs, 1),
                      (off[2], sc, 1, vs, 1)]
        else:
            raise ValueError(f'OSD: Format {fmt} nicht unterstuetzt')
        with self._cl:
            # Nur Plane-OFFSETS anders (z. B. Alignment-Padding zwischen den
            # Planes laut GstVideoMeta), Strides/Format/Groesse gleich? Dann
            # reicht ein Offset-Update -- die Fragmente (stride-abhaengig)
            # bleiben gueltig. WICHTIG: kein Voll-Rebake im Streaming-Thread,
            # der blockiert sonst 1-2 s die Pipeline (Client-Reconnect!).
            cheap = (self._layout is not None
                     and (fmt, W, H) == (self.fmt, self.W, self.H)
                     and [p[1] for p in planes]
                     == [p[1] for p in self.planes])
            self.fmt, self.W, self.H, self.planes = fmt, W, H, planes
            self.tables = self.btables = None  # alte Offsets sind ungueltig
            if self.grid_dims and not cheap:
                self._set_cell()               # neu einbacken, Grid bekannt
            self._layout = key
            self._last_grid = None             # naechstes Grid baut neu
        if cheap:
            log(f'OSD-Layout: Plane-Offsets aktualisiert ({fmt} {W}x{H})')
        else:
            log(f'OSD-Layout: {fmt} {W}x{H}, Zelle '
                + (f'{self.cell_w}x{self.cell_h}' if self.grid_dims
                   else 'folgt mit erstem FC-Grid'))

    def _set_cell(self):
        """Zellgroesse aus Bild UND Grid -- das Grid waehlt der User in INAV.

        Hoehe: Grid-Zeilen fuellen das Bild exakt (HD-Grid 20 Zeilen bei
        720p -> 36 px; SD-Grid 16 Zeilen bei 480p -> 30 px). Breite:
        2:3-Aspekt, geklemmt auf die Spaltenzahl -- sonst wird der
        Zentrier-Offset negativ und die Laeufe schreiben ausserhalb der
        Zeile. Gerade Werte wegen UYVY-Paaren bzw. Chroma-Subsampling.
        """
        rows, cols = self.grid_dims
        ch = self.H // rows
        if self.fmt != 'UYVY':
            ch &= ~1
        cw = min(ch * 2 // 3, self.W // cols) & ~1
        self.cell_w, self.cell_h = cw, ch
        self.tables = self.btables = None
        if (cw, ch) != self._font_cell:
            self._load_font()
        self._compile()
        log(f'OSD-Zellen: {cw}x{ch} fuer Grid {cols}x{rows} '
            f'auf {self.W}x{self.H}')

    def check_meta(self, meta):
        """GstVideoMeta gegen das angenommene Layout halten (Padding!)."""
        n = meta.n_planes
        key = (tuple(meta.offset[:n]), tuple(meta.stride[:n]))
        if key == self._meta_key:
            return
        self._meta_key = key
        self.set_layout(self.fmt, self.W, self.H,
                        offsets=key[0], strides=key[1])

    def caps_string(self):
        if self.fmt == 'UYVY':
            return 'video/x-raw,format=UYVY'
        return 'video/x-raw,format={ I420, Y42B }'

    def _fill_gaps(self, mask, g):
        """True-Luecken <= g Bytes je Zeile schliessen (vektorisiert)."""
        np = self.np
        n = mask.shape[1]
        idx = np.arange(n)
        big = n + g + 1
        last = np.where(mask, idx, -big)          # letzter True links von i
        np.maximum.accumulate(last, axis=1, out=last)
        nxt = np.where(mask[:, ::-1], idx, -big)  # naechster True rechts
        np.maximum.accumulate(nxt, axis=1, out=nxt)
        nxt = (n - 1) - nxt[:, ::-1]
        return mask | ((nxt - last) <= g + 1)

    # --- Font: PNG -> YUV+Alpha in Zellaufloesung -----------------------------
    def _load_font(self):
        import gi
        gi.require_version('GdkPixbuf', '2.0')
        from gi.repository import GdkPixbuf
        np = self.np
        pb = GdkPixbuf.Pixbuf.new_from_file(FONT_PATH)
        # Layout: 2 Glyphenspalten x 256 Zeilen. Variante egal -- skaliert wird
        # auf die Zellgroesse der aktuellen Aufloesung (downscale >> upscale,
        # also am besten die groesste Variante hochladen).
        if pb.get_height() % 256 or pb.get_width() % 2:
            raise ValueError(f'Font-PNG-Layout unbekannt '
                             f'({pb.get_width()}x{pb.get_height()})')
        if (pb.get_width() // 2, pb.get_height() // 256) != \
                (self.cell_w, self.cell_h):
            pb = pb.scale_simple(self.cell_w * 2, self.cell_h * 256,
                                 GdkPixbuf.InterpType.HYPER)
        if not pb.get_has_alpha():
            raise ValueError('Font-PNG hat keinen Alphakanal')
        w, h = pb.get_width(), pb.get_height()
        stride = pb.get_rowstride()
        raw = np.frombuffer(pb.get_pixels(), dtype=np.uint8)
        img = np.zeros((h, w, 4), dtype=np.uint8)
        for y in range(h):
            img[y] = raw[y * stride:y * stride + w * 4].reshape(w, 4)
        r = img[:, :, 0].astype(np.int32)
        g = img[:, :, 1].astype(np.int32)
        b = img[:, :, 2].astype(np.int32)
        yy = ((66 * r + 129 * g + 25 * b + 128) >> 8) + 16
        self._yy = np.clip(yy, 16, 235).astype(np.uint8)
        self._uu = (((-38 * r - 74 * g + 112 * b + 128) >> 8)
                    + 128).astype(np.uint8)
        self._vv = (((112 * r - 94 * g - 18 * b + 128) >> 8)
                    + 128).astype(np.uint8)
        self._aa = img[:, :, 3].copy()
        self._font_cell = (self.cell_w, self.cell_h)

    # --- Glyphen: Pixel -> Lauf-Fragmente fuers aktuelle Layout ---------------
    def _compile(self):
        np = self.np
        cw, ch = self.cell_w, self.cell_h

        # Drei Alpha-Klassen pro BYTE: opak (memcpy-Lauf), Saum (Blend-Lauf
        # mit echtem Alpha -- macht die vom Skalieren angefressenen Outlines
        # wieder weich), unsichtbar. Blend liest den Framebuffer (am CSI-Pfad
        # uncached!), bleibt aber billig, weil nur der schmale Saum betroffen
        # ist.
        A_OPAQUE, A_MIN = 224, 32

        def runs_from(mask, dst_stride, *layers):
            # Zeilenweise Laeufe; Trennspalte, sonst verschmilzt ein Lauf am
            # Zeilenende mit dem Zeilenanfang darunter und der memcpy
            # schmiert horizontal aus der Zelle (Linie bei Vollbreite-
            # Glyphen wie Crosshair/Horizont).
            hh, ww = mask.shape
            sep = np.zeros((hh, ww + 1), dtype=bool)
            sep[:, :ww] = mask
            flat = sep.reshape(-1)
            padded = np.concatenate(([False], flat, [False]))
            d = np.diff(padded.astype(np.int8))
            starts = np.flatnonzero(d == 1)
            lens = (np.flatnonzero(d == -1) - starts).astype(np.uint32)
            rel = ((starts // (ww + 1)) * dst_stride
                   + starts % (ww + 1)).astype(np.uint32)
            out = []
            for lay in layers:
                sepb = np.zeros((hh, ww + 1), dtype=np.uint8)
                sepb[:, :ww] = lay
                out.append(sepb.reshape(-1)[flat])
            return rel, lens, out

        pool = []                     # opake Bytes, alle Glyphen/Planes
        bval, balf = [], []           # Saum: Wert- und Alpha-Bytes, parallel
        offs = {'pool': 0, 'blend': 0}

        def fragment(values, alphas, dst_stride, gap):
            """Ein Plane-Fragment: opake + Saumlaeufe aus Wert/Alpha-Zellen.

            Saumlaeufe sind einzeln nur wenige Bytes lang; jede uncached-
            Lesetransaktion kostet aber ~500 ns Latenz (gemessen: 8400
            Einzellaeufe = 4,8 ms/Frame). Darum Luecken <= gap Bytes
            zwischen Saumstuecken einer Zeile MIT aufnehmen: opake Kerne
            blenden zu ihrem eigenen Wert (No-Op), Hintergrund hat Alpha ~0
            (Identitaet) -- wenige lange Reads statt vieler kurzer.
            """
            m_op = alphas >= A_OPAQUE
            m_bl = self._fill_gaps((alphas >= A_MIN) & ~m_op, gap)
            rel, lens, (gbytes,) = runs_from(m_op, dst_stride, values)
            src = (offs['pool'] + np.concatenate(([0], np.cumsum(lens)))
                   )[:len(lens)].astype(np.uint32)
            pool.append(gbytes)
            offs['pool'] += len(gbytes)
            rel_b, lens_b, (bb, ba) = runs_from(m_bl, dst_stride,
                                                values, alphas)
            src_b = (offs['blend'] + np.concatenate(([0], np.cumsum(lens_b)))
                     )[:len(lens_b)].astype(np.uint32)
            bval.append(bb)
            balf.append(ba)
            offs['blend'] += len(bb)
            return rel, src, lens, rel_b, src_b, lens_b

        # pro Glyphe: Liste von Fragmenten (eins je Plane) oder None
        glyphs = []
        for idx in range(512):
            gx, gy0 = (idx // 256) * cw, (idx % 256) * ch
            ga = self._aa[gy0:gy0 + ch, gx:gx + cw].astype(np.int32)
            if not (ga >= A_MIN).any():
                glyphs.append(None)
                continue
            gy = self._yy[gy0:gy0 + ch, gx:gx + cw]
            gu = self._uu[gy0:gy0 + ch, gx:gx + cw].astype(np.int32)
            gv = self._vv[gy0:gy0 + ch, gx:gx + cw].astype(np.int32)
            if self.fmt == 'UYVY':
                # UYVY-Zeile: pro Pixelpaar [U, Y0, V, Y1]. Chroma = alpha-
                # gewichtetes Mittel des Paars (ein transparenter Nachbar ist
                # meist schwarz und wuerde die Farbe sonst verfaelschen);
                # Chroma-Alpha = Mittel der Paar-Alphas.
                a0, a1 = ga[:, 0::2], ga[:, 1::2]
                asum = np.maximum(a0 + a1, 1)
                u_pair = ((gu[:, 0::2] * a0 + gu[:, 1::2] * a1)
                          // asum).astype(np.uint8)
                v_pair = ((gv[:, 0::2] * a0 + gv[:, 1::2] * a1)
                          // asum).astype(np.uint8)
                a_pair = (a0 + a1) // 2
                row_bytes = np.zeros((ch, cw * 2), dtype=np.uint8)
                row_alpha = np.zeros((ch, cw * 2), dtype=np.uint8)
                row_bytes[:, 0::4] = u_pair
                row_bytes[:, 1::4] = gy[:, 0::2]
                row_bytes[:, 2::4] = v_pair
                row_bytes[:, 3::4] = gy[:, 1::2]
                row_alpha[:, 0::4] = a_pair
                row_alpha[:, 1::4] = a0
                row_alpha[:, 2::4] = a_pair
                row_alpha[:, 3::4] = a1
                frags = [fragment(row_bytes, row_alpha,
                                  self.planes[0][1], 24)]
            else:
                # Planar: Y in voller Aufloesung, Chroma alpha-gewichtet auf
                # den Subsampling-Block gemittelt (2x2 bei I420, 2x1 bei
                # Y42B). Gap-Schwellen halbiert: 1 Byte/px statt 2.
                if self.planes[1][3]:            # vshift -> 2x2-Block
                    a_blk = (ga[0::2, 0::2] + ga[0::2, 1::2]
                             + ga[1::2, 0::2] + ga[1::2, 1::2])

                    def blk(x):
                        return (x[0::2, 0::2] * ga[0::2, 0::2]
                                + x[0::2, 1::2] * ga[0::2, 1::2]
                                + x[1::2, 0::2] * ga[1::2, 0::2]
                                + x[1::2, 1::2] * ga[1::2, 1::2])
                    n_blk = 4
                else:                            # 2x1-Paar (Y42B)
                    a_blk = ga[:, 0::2] + ga[:, 1::2]

                    def blk(x):
                        return x[:, 0::2] * ga[:, 0::2] + x[:, 1::2] * ga[:, 1::2]
                    n_blk = 2
                asum = np.maximum(a_blk, 1)
                u_c = (blk(gu) // asum).astype(np.uint8)
                v_c = (blk(gv) // asum).astype(np.uint8)
                a_c = (a_blk // n_blk).astype(np.uint8)
                frags = [
                    fragment(gy, ga.astype(np.uint8), self.planes[0][1], 12),
                    fragment(u_c, a_c, self.planes[1][1], 6),
                    fragment(v_c, a_c, self.planes[2][1], 6),
                ]
            glyphs.append(frags)
        z = np.zeros(0, dtype=np.uint8)
        self.pool = np.ascontiguousarray(np.concatenate(pool or [z]))
        self.bval = np.ascontiguousarray(np.concatenate(bval or [z]))
        self.balf = np.ascontiguousarray(np.concatenate(balf or [z]))
        self.pool_ptr = self.pool.ctypes.data_as(self.ctypes.c_void_p)
        self.bval_ptr = self.bval.ctypes.data_as(self.ctypes.c_void_p)
        self.balf_ptr = self.balf.ctypes.data_as(self.ctypes.c_void_p)
        self.glyphs = glyphs
        self.n_glyphs = sum(1 for g in glyphs if g is not None)

    # --- Grid-Paket -> flache Lauftabelle -------------------------------------
    def rebuild(self, rows, cols, grid):
        # INAV zeichnet mit ~46 Hz, meist ohne inhaltliche Aenderung --
        # identische Grids kosten dann nur diesen Vergleich.
        key = (rows, cols, grid)
        if key == self._last_grid:
            return
        with self._cl:
            self._rebuild(rows, cols, grid, key)

    def _rebuild(self, rows, cols, grid, key):
        if not (0 < rows <= 32 and 0 < cols <= 64):
            return
        if (rows, cols) != self.grid_dims:
            # Adaptive Zellgroesse: der FC bestimmt das Grid (HD 53x20,
            # SD 30x16, ...) -- Wechsel backt Font + Fragmente neu ein
            # (einmalig ~1 s, OSD ist waehrenddessen kurz weg).
            self.grid_dims = (rows, cols)
            self._set_cell()
        self._last_grid = key
        np = self.np
        # Canvas zentrieren (auch SD-Grids landen mittig im Bild); Ursprung
        # gerade halten (UYVY-Paare bzw. Chroma-Subsampling-Raster).
        x0 = (self.W - cols * self.cell_w) // 2 & ~1
        y0 = (self.H - rows * self.cell_h) // 2
        if self.fmt != 'UYVY':
            y0 &= ~1
        if x0 < 0 or y0 < 0:
            # Grid passt nicht ins Bild -- lieber kein OSD als korrupte
            # Laeufe ausserhalb der Zeilen (zerrissenes Bild).
            self.tables = self.btables = None
            return
        parts, bparts = [], []
        for i, glyph_idx in enumerate(grid):
            if not glyph_idx:
                continue
            g = self.glyphs[glyph_idx] if glyph_idx < 512 else None
            if g is None:
                continue
            r, c = divmod(i, cols)
            px = x0 + c * self.cell_w
            py = y0 + r * self.cell_h
            for (off, stride, hs, vs, bpp), frag in zip(self.planes, g):
                base = np.uint32(off + (py >> vs) * stride
                                 + (px >> hs) * bpp)
                rel, src, lens, rel_b, src_b, lens_b = frag
                if len(lens):
                    parts.append((rel + base, src, lens))
                if len(lens_b):
                    bparts.append((rel_b + base, src_b, lens_b))

        def flatten(plist, *pools):
            # Snapshot-Tupel: Laeufe UND die dazugehoerigen Pool-Pointer
            # derselben Compile-Generation -- stamp() liest lock-frei und
            # darf nie neue Laeufe mit alten Pools mischen (oder umgekehrt).
            if not plist:
                return None
            runs = np.ascontiguousarray(np.column_stack(
                [np.concatenate([p[j] for p in plist]) for j in range(3)]
            ).astype(np.uint32).reshape(-1))
            return (runs.ctypes.data_as(self.ctypes.c_void_p),
                    len(runs) // 3, runs) + pools

        self.tables = flatten(parts, self.pool_ptr, self.pool)
        self.btables = flatten(bparts, self.bval_ptr, self.balf_ptr,
                               self.bval, self.balf)

    def _start_listener(self):
        import socket
        import threading

        def run():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('127.0.0.1', OSD_UDP_PORT))
            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    if len(data) < 8 or data[:4] != b'UOSD':
                        continue
                    rows, cols = data[5], data[6]
                    n = rows * cols
                    if len(data) < 8 + 2 * n:
                        continue
                    grid = struct.unpack_from(f'<{n}H', data, 8)
                    self.rebuild(rows, cols, grid)
                except Exception as e:   # noqa: BLE001 -- Stream schuetzen
                    log(f'OSD-Listener: {e}')
                    time.sleep(1)

        threading.Thread(target=run, daemon=True).start()

    def stamp(self, addr):
        t0 = time.monotonic() if self._timing else 0.0
        t = self.tables
        if t:
            self.lib.stamp(addr, t[3], t[0], t[1])
        b = self.btables
        if b:
            self.lib.blend(addr, b[3], b[4], b[0], b[1])
        if self._timing:
            dt = time.monotonic() - t0
            self._tsum += dt
            self._tmax = max(self._tmax, dt)
            self._tn += 1
            if self._tn >= 600:
                log(f'osd-timing: avg {self._tsum / self._tn * 1e3:.2f} ms, '
                    f'max {self._tmax * 1e3:.2f} ms, '
                    f'runs {t[1] if t else 0}+{b[1] if b else 0}')
                self._tsum = self._tmax = 0.0
                self._tn = 0


def register_osd_element(engine):
    """osdstamp-Element registrieren (einmalig, vor dem Pipeline-Parse)."""
    import gi
    gi.require_version('GstBase', '1.0')
    gi.require_version('GstVideo', '1.0')
    from gi.repository import GstBase, GstVideo, GObject

    caps = Gst.Caps.from_string(engine.caps_string())

    class OsdStamp(GstBase.BaseTransform):
        __gstmetadata__ = ('osdstamp', 'Filter/Video',
                           'UAV-Link OSD burn-in (masked memcpy)', 'uav-link')
        __gsttemplates__ = (
            Gst.PadTemplate.new('sink', Gst.PadDirection.SINK,
                                Gst.PadPresence.ALWAYS, caps),
            Gst.PadTemplate.new('src', Gst.PadDirection.SRC,
                                Gst.PadPresence.ALWAYS, caps),
        )

        def do_set_caps(self, incaps, outcaps):
            # Ausgehandeltes Format uebernehmen (z. B. Y42B statt I420,
            # wenn die Kamera 4:2:2-JPEGs liefert). Ueber den Caps-STRING:
            # PyGIs Structure-Wrapper hat kein get_string/get_value.
            try:
                s = incaps.to_string()
                fmt = re.search(r'format=\(string\)(\w+)', s).group(1)
                w = int(re.search(r'width=\(int\)(\d+)', s).group(1))
                h = int(re.search(r'height=\(int\)(\d+)', s).group(1))
                engine.set_layout(fmt, w, h)
            except Exception as e:   # noqa: BLE001 -- Stream schuetzen
                log(f'osdstamp set_caps: {e}')
            return True

        def do_transform_ip(self, buf):
            try:
                # Gepolsterte Puffer (Stride/Offset laut Meta != dicht
                # gepackt) einmalig ins Layout uebernehmen -- sonst
                # schreiben die Laeufe an die falschen Stellen.
                meta = GstVideo.buffer_get_video_meta(buf)
                if meta is not None:
                    engine.check_meta(meta)
                ok, m = buf.map(Gst.MapFlags.READ | Gst.MapFlags.WRITE)
                if ok:
                    cbuf = (engine.ctypes.c_ubyte * m.size).from_buffer(m.data)
                    engine.stamp(engine.ctypes.addressof(cbuf))
                    buf.unmap(m)
            except Exception as e:       # noqa: BLE001 -- Stream schuetzen
                log(f'osdstamp: {e}')
            return Gst.FlowReturn.OK

    GObject.type_register(OsdStamp)
    if not Gst.Element.register(None, 'osdstamp', Gst.Rank.NONE, OsdStamp):
        raise RuntimeError('osdstamp-Registrierung fehlgeschlagen')


# Fuers Web-UI gepflegte Schnappschuss-Dateien (Preview OHNE RTSP-Client)
KEYFRAME_PATH = '/tmp/uav-keyframe.h264'
PREVIEW_SRC_PATH = '/tmp/uav-preview-src.jpg'


class StreamHub:
    """Persistente Capture-Pipeline -> Frames an die RTSP-Media(s) verteilen.

    Warum (gemessen 15.08., GST_DEBUG rtspmedia:5): gst-rtsp-server faehrt
    die GETEILTE Media bei jedem Client-SETUP/PLAY UND jedem TEARDOWN durch
    PAUSED -- fuer v4l2src heisst das STREAMOFF. Die CSI-Bridge verkraftet
    das unsichtbar schnell, eine UVC-Webcam braucht danach 5-20 s (USB-
    Renegotiation + Autoexposure-Konvergenz). Client-Wechsel (insbesondere
    Kite-Auto-Reconnects, die Session-Leichen ohne TEARDOWN hinterlassen)
    wuergten die Kamera so in einer selbsterhaltenden Freeze-Schleife ab.

    Quelle und Encoder laufen HIER, aber LAZY: gestartet beim ersten
    Client, gestoppt GRACE_S Sekunden nach dem letzten -- im Leerlauf
    null Systemlast und Kamera aus (die permanente Variante kostete
    konstant ~1,5 Kerne, 15.08. abends zurueckgebaut). Die Nachlauffrist
    ueberbrueckt Client-Reconnects (Kite!), ohne die Kamera zu stoppen;
    solange ein Client dranbleibt, treffen PAUSED-Zyklen anderer Clients
    weiterhin nur appsrc. Waehrend des Betriebs liegt das juengste
    Keyframe (h264) bzw. JPEG in /tmp fuer die Web-UI-Preview (im
    Leerlauf veraltet sie entsprechend).
    """

    GRACE_S = 15                       # Nachlauf nach letztem Client

    def __init__(self, launch, is_h264):
        self.lock = threading.Lock()
        self.srcs = {}                 # appsrc -> darf schon Frames sehen
        self.is_h264 = is_h264
        self._last_save = 0.0
        self._running = False
        self._stop_id = 0              # anstehender Nachlauf-Stopp (GLib)
        self._got_frame = threading.Event()
        self.pipe = Gst.parse_launch(launch)
        sink = self.pipe.get_by_name('vidsink')
        sink.connect('new-sample', self._on_sample)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()
        bus.connect('message::error', self._on_error)
        # Kein PLAYING hier -- Capture startet erst mit dem ersten Client.

    def _on_error(self, bus, msg):
        err, _ = msg.parse_error()
        log(f'CAPTURE-FEHLER: {err.message} -- Neustart via systemd')
        os.kill(os.getpid(), signal.SIGINT)

    def attach(self, media):
        s = media.get_element().get_by_name('vidsrc')
        if s is None:
            return
        # h264: erst ab dem naechsten Keyframe fuettern (sauberer Einstieg,
        # SPS/PPS haengen dank config-interval=-1 direkt davor). JPEG:
        # jedes Frame steht fuer sich.
        with self.lock:
            self.srcs[s] = not self.is_h264
            if self._stop_id:
                GLib.source_remove(self._stop_id)
                self._stop_id = 0
            start = not self._running
            self._running = True
        if start:
            log('Capture startet (erster Client)')
            self.pipe.set_state(Gst.State.PLAYING)
        # Kaltstart abfedern: ohne fliessende Frames hat h264parse in der
        # Media noch keine Caps und das SDP der ALLERERSTEN DESCRIBE-
        # Anfrage schluege fehl (503). Begrenzt warten, bis die Quelle
        # liefert (UVC braucht nach dem Start 1-5 s); klappt es nicht,
        # faengt der Client-Retry den Rest.
        if start and not self._got_frame.wait(8):
            log('Capture: Quelle liefert noch nichts (Kaltstart-Timeout)')
        media.connect('unprepared', self._detach, s)

    def _detach(self, media, s):
        with self.lock:
            self.srcs.pop(s, None)
            if self.srcs or not self._running or self._stop_id:
                return
            self._stop_id = GLib.timeout_add_seconds(self.GRACE_S,
                                                     self._idle_stop)

    def _idle_stop(self):
        with self.lock:
            self._stop_id = 0
            if self.srcs or not self._running:
                return False           # doch wieder Publikum -- weiterlaufen
            self._running = False
        log(f'Capture stoppt ({self.GRACE_S} s ohne Client)')
        self._got_frame.clear()
        self.pipe.set_state(Gst.State.NULL)
        return False

    def _on_sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        ok, m = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        data = bytes(m.data)
        buf.unmap(m)
        self._got_frame.set()
        if key or not self.is_h264:
            self._save_snapshot(data)
        with self.lock:
            if self.is_h264 and key:
                for s in self.srcs:
                    self.srcs[s] = True
            targets = [s for s, ready in self.srcs.items() if ready]
        for s in targets:
            # Frische Buffer OHNE Timestamps pushen -- appsrc (do-timestamp)
            # stempelt sie mit der Running-Time der jeweiligen Media.
            # (Capture-PTS direkt durchreichen scheitert an der fremden
            # Base-Time; PTS nullen scheitert an PyGIs Writability-Sperre.)
            s.emit('push-buffer', Gst.Buffer.new_wrapped(data))
        return Gst.FlowReturn.OK

    def _save_snapshot(self, data):
        now = time.monotonic()
        if now - self._last_save < 1.0:
            return
        self._last_save = now
        path = KEYFRAME_PATH if self.is_h264 else PREVIEW_SRC_PATH
        try:
            with open(path + '.tmp', 'wb') as f:
                f.write(data)
            os.replace(path + '.tmp', path)
        except OSError:
            pass


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
    global OSD_ENGINE
    OSD_ENGINE = OsdEngine.create(cfg, src)
    if OSD_ENGINE:
        try:
            register_osd_element(OSD_ENGINE)
        except Exception as e:           # noqa: BLE001 -- Stream schuetzen
            log(f'OSD-Element-Registrierung fehlgeschlagen: {e}')
            OSD_ENGINE = None
    capture, media_launch, is_h264 = build_pipelines(cfg, src)
    hub = StreamHub(capture, is_h264)
    log('Capture-Pipeline lazy: startet mit dem ersten Client, stoppt '
        f'{StreamHub.GRACE_S} s nach dem letzten (s. StreamHub)')
    server = GstRtspServer.RTSPServer()
    server.set_service(str(cfg['port']))
    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_launch(media_launch)
    # NICHT geteilt: jeder Client bekommt seine private Mini-Media (appsrc ->
    # parse -> pay, ohne Encoder = praktisch gratis), der StreamHub fuettert
    # alle parallel. Der alte Grund fuer set_shared(True) -- die Quelle ist
    # nur einmal oeffenbar -- ist mit der persistenten Capture obsolet. Und
    # WICHTIG: bei geteilter Media treffen die PAUSED-Zyklen, die gst-rtsp-
    # server bei jedem Client-SETUP/PLAY/TEARDOWN faehrt, ALLE Zuschauer
    # (gemessen 15.08.: 1-6 s Luecken bei jedem Join/Leave). Private Medias
    # stoeren nur den Client, der gerade kommt oder geht.
    factory.set_shared(False)
    factory.set_latency(0)
    factory.set_protocols(GstRtsp.RTSPLowerTrans.UDP)  # kein TCP-Fallback, kein Resend
    factory.connect('media-configure', on_media_configure, cfg)
    factory.connect('media-configure', lambda f, m: hub.attach(m))
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
