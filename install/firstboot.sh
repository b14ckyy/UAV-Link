#!/bin/bash
# UAV-Link first-boot installer trigger — boot-partition drop-in. No console needed.
#
# SETUP after flashing stock Raspberry Pi OS (use the Imager dialog for user +
# Wi-Fi with internet + SSH):
#   1. Copy THIS file to the boot partition (the small FAT drive). Keep the name.
#   2. Append one token to the END of cmdline.txt on that partition (single line,
#      leading space, NO newline):
#         systemd.run=/boot/firmware/firstboot.sh
#   3. Boot. It installs over Wi-Fi and reboots, fully configured.
#      Progress log: uav-setup.log on the boot partition.
#
# WHY TWO STAGES: the kernel `systemd.run=` hook boots into a minimal target with no
# network and no cloud-init. So stage 1 (this early run) only schedules a proper
# systemd service and reboots; stage 2 runs that service in a normal, networked boot.
# It all happens automatically — runs exactly once.

REF="main"        # branch or release tag to install (pin a tag for reproducible images)

BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
exec >>"$BOOT/uav-setup.log" 2>&1

# ================= STAGE 2: real install (normal boot, network up) =================
if [ "${1:-}" = install ]; then
    echo "[firstboot] stage 2: installing (ref=$REF)"
    [ -f "$BOOT/.uav-installed" ] && exit 0
    # wait until cloud-init has finished provisioning the user (fresh flashes)
    command -v cloud-init >/dev/null 2>&1 && cloud-init status --wait >/dev/null 2>&1
    for i in $(seq 1 60); do getent hosts github.com >/dev/null 2>&1 && break; sleep 2; done
    cd /opt 2>/dev/null || cd /tmp
    if curl -fsSL "https://github.com/b14ckyy/UAV-Link/archive/refs/heads/$REF.tar.gz" -o uav.tgz \
       || curl -fsSL "https://github.com/b14ckyy/UAV-Link/archive/refs/tags/$REF.tar.gz" -o uav.tgz; then
        tar -xzf uav.tgz
        if bash UAV-Link-*/install/install.sh; then
            touch "$BOOT/.uav-installed"
            systemctl disable uav-firstboot.service 2>/dev/null
            rm -f /etc/systemd/system/uav-firstboot.service
            echo "[firstboot] install OK — rebooting"
            sync; sleep 2; reboot
        else
            echo "[firstboot] installer returned non-zero — see log above"
        fi
    else
        echo "[firstboot] download failed (no internet? wrong REF=$REF?)"
    fi
    exit 0
fi

# ================= STAGE 1: schedule (minimal target, no network) =================
echo "[firstboot] stage 1: scheduling networked installer (ref=$REF)"
mount -o remount,rw / 2>/dev/null || true
# remove our one-shot cmdline token so the next boot is a normal boot
sed -i 's# systemd.run=[^ ]*firstboot.sh##g' "$BOOT/cmdline.txt" 2>/dev/null
[ -f "$BOOT/.uav-installed" ] && { echo "[firstboot] already installed"; exit 0; }
# copy self to a real path and install a oneshot that runs after network next boot
install -m 755 "$0" /usr/local/sbin/uav-firstboot 2>/dev/null || cp "$0" /usr/local/sbin/uav-firstboot
cat > /etc/systemd/system/uav-firstboot.service <<EOF
[Unit]
Description=UAV-Link first-boot installer
After=network-online.target cloud-final.service
Wants=network-online.target
ConditionPathExists=!$BOOT/.uav-installed
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/uav-firstboot install
[Install]
WantedBy=multi-user.target
EOF
systemctl enable uav-firstboot.service 2>/dev/null
echo "[firstboot] stage 1 done — rebooting into normal boot"
sync; sleep 2; reboot
