#!/bin/bash
# UAV-Link idempotent installer.
#
#   sudo bash install/install.sh          # manual (from a clone or extracted release)
#   (also run as root by the cloud-init first-boot bootstrap — see user-data.example)
#
# Safe to re-run: existing config/credentials are preserved, boot-config edits are
# added only once, packages/services/venv are only touched if needed. Reboot after
# the first run (UART / I2C / dwc2 overlays need it).
set -uo pipefail

log()  { echo "[uav-install] $*"; }
warn() { echo "[uav-install] WARN: $*" >&2; }

[ "$(id -u)" = 0 ] || { echo "Please run with sudo/root."; exit 1; }

# --- target user + directories -------------------------------------------------
if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    UAV_USER="$SUDO_USER"
else
    UAV_USER="$(id -un 1000 2>/dev/null || true)"
fi
[ -n "${UAV_USER:-}" ] || { echo "Could not determine target user."; exit 1; }
UAV_HOME="$(getent passwd "$UAV_USER" | cut -d: -f6)"
UAV_DIR="$UAV_HOME/uav-link"
REPO="$(cd "$(dirname "$0")/.." && pwd)"      # install/ lives under the repo root
[ -d "$REPO/air-unit" ] || { echo "Repo layout not found next to install/ ($REPO)"; exit 1; }
log "user=$UAV_USER  dir=$UAV_DIR  repo=$REPO"

# --- 1. packages ---------------------------------------------------------------
log "installing packages (apt)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q || warn "apt update failed (offline?)"
apt-get install -y --no-install-recommends \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-rtsp \
    gir1.2-gst-rtsp-server-1.0 python3-gi v4l-utils \
    python3-flask python3-venv python3-gpiozero python3-lgpio \
    python3-numpy python3-gst-1.0 gir1.2-gdkpixbuf-2.0 \
    modemmanager libqmi-utils network-manager nftables wireguard-tools socat \
    i2c-tools \
    || warn "some packages failed to install"

# --- 2. code into $UAV_DIR -----------------------------------------------------
log "deploying code to $UAV_DIR"
install -d -o "$UAV_USER" -g "$UAV_USER" "$UAV_DIR"
# --remove-destination ist hier wichtig, nicht kosmetisch: uav-update laeuft
# WAEHREND dieses Skript ausgeführt wird (es hat den Installer ja gestartet).
# Ohne das Flag ueberschreibt cp die Datei an Ort und Stelle, also im selben
# Inode -- und bash liest Skripte fortlaufend nach. Der laufende Updater fuehrt
# dann Bruchstuecke des neuen Inhalts aus ("bal: command not found"). Mit dem
# Flag wird das Ziel vorher entfernt, die laufende Datei behaelt ihren Inode.
cp -a --remove-destination "$REPO/air-unit/." "$UAV_DIR/"
# never clobber the operator's runtime config / credentials
[ -f "$UAV_DIR/config.json" ] || cp "$UAV_DIR/config.example.json" "$UAV_DIR/config.json"
# record version metadata as JSON. uav-update passes channel/commit/commit-date;
# fall back to git or the extracted files' mtime (git archive stamps commit time).
REF="$(basename "$REPO")"; REF="${REF#UAV-Link-}"
CH="${UAV_CHANNEL:-manual}"
COMMIT="${UAV_COMMIT:-}"
CDATE="${UAV_COMMIT_DATE:-}"
if [ -z "$COMMIT" ] && command -v git >/dev/null 2>&1 && git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    COMMIT="$(git -C "$REPO" rev-parse --short=7 HEAD 2>/dev/null)"
    [ -n "$CDATE" ] || CDATE="$(git -C "$REPO" log -1 --format=%cd --date=format:'%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
fi
[ -n "$COMMIT" ] || COMMIT="unknown"
[ -n "$CDATE" ] || CDATE="$(date -u -d "@$(stat -c %Y "$0" 2>/dev/null || echo 0)" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)"
printf '{"channel":"%s","ref":"%s","commit":"%s","commit_date":"%s","updated":"%s"}\n' \
    "$CH" "$REF" "$COMMIT" "$CDATE" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$UAV_DIR/VERSION"
chown -R "$UAV_USER:$UAV_USER" "$UAV_DIR"
chmod +x "$UAV_DIR/uav-wg-apply" "$UAV_DIR/uav-update" "$UAV_DIR/uav-backup" "$UAV_DIR/uav-hdmi-setup" "$UAV_DIR/uav-recorder" "$UAV_DIR/reset-credentials.sh" "$UAV_DIR"/test/*.sh 2>/dev/null

# OSD-Stanzer kompilieren (winzige .so; gcc ist Teil des Basis-Images).
# Idempotent: nur wenn die .so fehlt oder die Quelle neuer ist.
if [ "$UAV_DIR/osd-stamp.c" -nt "$UAV_DIR/libosdstamp.so" ] 2>/dev/null \
        || [ ! -f "$UAV_DIR/libosdstamp.so" ]; then
    log "compiling OSD stamper..."
    gcc -O3 -shared -fPIC -o "$UAV_DIR/libosdstamp.so" "$UAV_DIR/osd-stamp.c" \
        && chown "$UAV_USER:$UAV_USER" "$UAV_DIR/libosdstamp.so" \
        || warn "OSD stamper build failed (OSD burn-in will stay off)"
fi

# --- 3. OLED venv (luma.oled) --------------------------------------------------
if [ ! -x "$UAV_DIR/oled-venv/bin/python" ]; then
    log "creating luma.oled venv..."
    sudo -u "$UAV_USER" python3 -m venv "$UAV_DIR/oled-venv" \
        && sudo -u "$UAV_USER" "$UAV_DIR/oled-venv/bin/pip" install -q --upgrade pip \
        && sudo -u "$UAV_USER" "$UAV_DIR/oled-venv/bin/pip" install -q luma.oled \
        || warn "luma.oled venv setup failed (OLED optional)"
fi

# --- 4. groups for HW access ---------------------------------------------------
for g in video render dialout netdev gpio i2c spi plugdev; do
    getent group "$g" >/dev/null 2>&1 && usermod -aG "$g" "$UAV_USER"
done

# --- 5. systemd units (fill placeholders) --------------------------------------
log "installing systemd units..."
for f in "$REPO"/systemd/*.service; do
    sed "s#__UAVLINK_USER__#$UAV_USER#g; s#__UAVLINK_DIR__#$UAV_DIR#g" "$f" \
        > "/etc/systemd/system/$(basename "$f")"
done
systemctl daemon-reload

# --- 6. sudoers ----------------------------------------------------------------
log "installing sudoers rule..."
sed "s#__UAVLINK_USER__#$UAV_USER#g; s#__UAVLINK_DIR__#$UAV_DIR#g" \
    "$REPO/system/sudoers.d-uav-web" > /etc/sudoers.d/uav-web
chmod 0440 /etc/sudoers.d/uav-web
visudo -cf /etc/sudoers.d/uav-web || { warn "sudoers invalid — removing"; rm -f /etc/sudoers.d/uav-web; }

# --- 7. nftables LTE-hardening firewall ----------------------------------------
log "installing nftables firewall..."
cp "$REPO/air-unit/uav-firewall.nft" /etc/nftables.conf
systemctl enable nftables >/dev/null 2>&1

# --- 7b. udev: ModemManager weg von den AT-Ports (USB-Stoerung, s. Datei) ------
log "installing udev rule (ModemManager QMI-only)..."
cp "$REPO/system/udev-99-uav-mm-ignore-at.rules" \
    /etc/udev/rules.d/99-uav-mm-ignore-at.rules

# --- 8. boot config (config.txt / cmdline.txt) ---------------------------------
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
CMD=/boot/firmware/cmdline.txt; [ -f "$CMD" ] || CMD=/boot/cmdline.txt
if [ -f "$CFG" ]; then
    log "boot config: $CFG"
    [ -f "$CFG.uav-bak" ] || cp "$CFG" "$CFG.uav-bak"
    # Unsere Zeilen landen per >> am Dateiende. Damit sie fuer JEDES Modell
    # gelten, muss dort ein [all] stehen -- sonst faellt alles unter den letzten
    # Modellfilter der Stock-Datei ([pi5] o. ae.) und wirkt auf einem Zero 2 W nie.
    grep -qxF '[all]' "$CFG" || echo '[all]' >> "$CFG"
    # Nur AB [all] auf Vorhandensein pruefen, nicht in der ganzen Datei: das
    # Stock-Image hat "dtoverlay=dwc2,dr_mode=host" bereits unter [cm5] stehen.
    # Ein grep ueber die gesamte Datei findet die Zeile und ueberspringt sie --
    # der OTG-Host-Modus wurde auf dem Zero 2 W dadurch NIE gesetzt (13.08. bemerkt).
    add_cfg() {
        sed -n '/^\[all\]/,$p' "$CFG" | grep -qxF "$1" || echo "$1" >> "$CFG"
    }
    # Bestehenden Schluessel ersetzen statt anhaengen: ein zweites
    # camera_auto_detect= weiter unten wuerde das erste wieder aushebeln.
    set_cfg() {
        if grep -qE "^[#[:space:]]*$1=" "$CFG"; then
            sed -i "s/^[#[:space:]]*$1=.*/$1=$2/" "$CFG"
        else
            echo "$1=$2" >> "$CFG"
        fi
    }
    add_cfg "dtoverlay=dwc2,dr_mode=host"   # USB OTG host for CVBS dongle
    add_cfg "enable_uart=1"                 # PL011 UART for MSP
    add_cfg "dtoverlay=disable-bt"          # free the UART
    add_cfg "dtparam=i2c_arm=on"            # OLED I2C
    # HDMI->CSI-Bridge (Toshiba TC358743). Ohne HAT laeuft der Treiber ins Leere
    # und bindet einfach nicht -- deshalb koennen wir das unbesehen setzen, auch
    # auf Geraeten, die nie eine Bridge sehen. camera_auto_detect findet nur
    # offizielle Pi-Kameras ueber deren EEPROM; eine HDMI-Bridge hat keins, also
    # muss das Overlay von Hand kommen. ACHTUNG: Damit ist der CSI-Anschluss fuer
    # eine offizielle Pi-Kamera belegt -- UAV-Link unterstuetzt die ohnehin nicht.
    add_cfg "dtoverlay=tc358743"
    set_cfg "camera_auto_detect" 0
else
    warn "no config.txt found — skipping boot overlays"
fi
if [ -f "$CMD" ]; then
    [ -f "$CMD.uav-bak" ] || cp "$CMD" "$CMD.uav-bak"
    sed -i 's/console=serial0,[0-9]* //' "$CMD"   # free the serial console
fi
# i2c-dev legt die /dev/i2c-* Knoten an. Ohne das Modul findet i2cdetect gar
# keinen Bus -- man scannt dann ins Leere und haelt das faelschlich fuer
# "Chip antwortet nicht". Rein fuer die Diagnose, der Betrieb braucht es nicht.
grep -qxF i2c-dev /etc/modules 2>/dev/null || echo i2c-dev >> /etc/modules
modprobe i2c-dev 2>/dev/null || true

# --- 9. WWAN profile (empty APN -> configured later via web UI) ----------------
if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx uav-wwan; then
    log "creating WWAN profile uav-wwan (APN empty; set it in the web UI)"
    nmcli connection add type gsm con-name uav-wwan gsm.apn "" \
        ipv6.method disabled connection.autoconnect yes >/dev/null 2>&1 \
        || warn "WWAN profile not created (no modem yet? set up later via web UI)"
fi
udevadm control --reload-rules >/dev/null 2>&1 && udevadm trigger >/dev/null 2>&1

# --- 10. enable + start services -----------------------------------------------
log "enabling services..."
systemctl enable --now \
    uav-wifi-on.service uav-wifi-fallback.service uav-hdmi.service \
    uav-rtsp.service uav-web.service uav-msp.service uav-oled.service \
    uav-recorder.service uav-osd.service \
    >/dev/null 2>&1 || warn "some services failed to start (may need the reboot)"
# `enable --now` no-ops on an already-running unit, so an update would deploy new
# code but keep the old process. Restart the long-running services so updates take
# effect immediately. Detached-safe: updates run under uav-update@.service, so
# restarting uav-web does not kill the running updater. try-restart skips units
# that aren't running (e.g. uav-oled without OLED hardware).
for s in uav-rtsp uav-msp uav-oled uav-web uav-recorder uav-osd; do
    systemctl try-restart "$s.service" >/dev/null 2>&1 || warn "restart $s failed"
done
# Ein hinterlegter WireGuard-Tunnel muss einen Reboot ueberleben. Aeltere
# Installationen haben die Unit nie aktiviert bekommen (uav-wg-apply hat nur
# restartet) — ein Update heilt das hier nachtraeglich. Bewusst nur enable und
# kein start: der Tunnel ist Full-Tunnel (AllowedIPs 0.0.0.0/0), den mitten im
# laufenden Update hochzuziehen wuerde am Routing der Verbindung drehen, ueber
# die das Update selbst laeuft. Wirksam wird es beim naechsten Boot — und genau
# der war ja das Problem.
if [ -f /etc/wireguard/wgnet.conf ] \
   && [ "$(systemctl is-enabled wg-quick@wgnet 2>/dev/null)" != enabled ]; then
    log "activating wg-quick@wgnet for boot (config present but unit was not enabled)"
    systemctl enable wg-quick@wgnet >/dev/null 2>&1 || warn "wg-quick enable failed"
fi

# --- 11. provisioning done: disable cloud-init for faster subsequent boots -----
[ -d /etc/cloud ] && touch /etc/cloud/cloud-init.disabled

log "DONE. Reboot to activate UART/I2C/dwc2, then open http://<pi>:8080"
log "  (default web password: uavlink2026 — you'll be prompted to change it)"
