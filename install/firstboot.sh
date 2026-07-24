#!/bin/bash
# UAV-Link first-boot installer trigger — boot-partition drop-in. No console needed.
#
# SETUP after flashing stock Raspberry Pi OS (use the Imager dialog for user +
# Wi-Fi with internet + SSH):
#   1. Copy THIS file to the boot partition (the small FAT drive), keep the name.
#   2. Append one token to the END of cmdline.txt on that same partition
#      (it is a single line — add a leading space, do NOT add a newline):
#         systemd.run=/boot/firmware/firstboot.sh
#   3. Boot. It installs over Wi-Fi and reboots, fully configured.
#      Progress log: uav-setup.log on the boot partition.
#
# It runs exactly once (removes its own cmdline token + drops a marker), then the
# normal system boots. Edit REF below to pin a release tag for reproducible images.

REF="main"        # branch or release tag to install

set -uo pipefail
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
exec >>"$BOOT/uav-setup.log" 2>&1
echo "[firstboot] $(cat /proc/uptime 2>/dev/null | cut -d' ' -f1)s: start (ref=$REF)"

# --- one-shot guards: strip our cmdline token, honour a marker ---
sed -i 's# systemd.run=[^ ]*firstboot.sh##g' "$BOOT/cmdline.txt" 2>/dev/null
if [ -f "$BOOT/.uav-installed" ]; then
    echo "[firstboot] marker present — already installed, skipping"
    exit 0
fi

# --- wait for connectivity (apt/pip need it) ---
for i in $(seq 1 120); do
    getent hosts github.com >/dev/null 2>&1 && break
    sleep 2
done

# --- fetch the payload (branch first, then tag) and run the installer ---
cd /opt 2>/dev/null || cd /tmp
if curl -fsSL "https://github.com/b14ckyy/UAV-Link/archive/refs/heads/$REF.tar.gz" -o uav.tgz \
   || curl -fsSL "https://github.com/b14ckyy/UAV-Link/archive/refs/tags/$REF.tar.gz" -o uav.tgz; then
    tar -xzf uav.tgz
    if bash UAV-Link-*/install/install.sh; then
        touch "$BOOT/.uav-installed"
    else
        echo "[firstboot] installer returned non-zero — see log above"
    fi
else
    echo "[firstboot] download failed (no internet? wrong REF=$REF?)"
fi

echo "[firstboot] rebooting"
sync
sleep 2
reboot
