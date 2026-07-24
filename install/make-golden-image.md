# Building the Vanilla / golden image

A clean, personal-data-free image users can flash and update from the web UI. Set it up
once on a Pi, strip identifying/stale state, pull a disk image.

## 1. Base install (over LAN — no Wi-Fi credentials get baked in)

- Flash stock Raspberry Pi OS with Raspberry Pi Imager. In the customization dialog set:
  username **`link`**, password **`uavlink2026`**, hostname **`uav-link`**, SSH **enabled**.
  Do **not** set Wi-Fi — connect the Pi by **Ethernet (USB-LAN adapter)** instead.
- Boot, then run the installer (only the 4G HAT needs to be attached; other hardware is
  optional — the installer tolerates its absence):
  ```bash
  curl -fsSL https://github.com/b14ckyy/UAV-Link/archive/refs/heads/main.tar.gz | tar -xz
  sudo bash UAV-Link-main/install/install.sh
  ```
- Do **not** configure APN, WireGuard, camera, or a new password — leave everything default.

## 2. Strip state so clones are clean and unique (run right before imaging)

```bash
# no personal secrets / config in the image
sudo rm -f /home/link/uav-link/webui-auth.json      # -> web UI recreates default pw
sudo rm -f /etc/wireguard/wgnet.conf                # -> user pastes their own
sudo nmcli connection modify uav-wwan gsm.apn "" 2>/dev/null || true
cp /home/link/uav-link/config.example.json /home/link/uav-link/config.json
sudo chown link:link /home/link/uav-link/config.json

# no baked Wi-Fi client profiles (AP-only base state)
for c in $(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"&&$1!="uav-hotspot"{print $1}'); do sudo nmcli connection delete "$c"; done

# no update leftovers / caches baked into the image
rm -rf /home/link/UAV-Link-*                          # manual tarball extraction dirs
sudo rm -rf /tmp/tmp.*                                # interrupted uav-update temp dirs
sudo truncate -s 0 /var/log/uav-update.log           # your test-update history
sudo apt-get clean                                   # apt .deb cache (shrinks the image)

# unique per clone
sudo truncate -s 0 /etc/machine-id
sudo rm -f /etc/ssh/ssh_host_*                       # regenerated on first boot
sudo rm -f /boot/firmware/.uav-installed /boot/firmware/uav-setup.log 2>/dev/null

sudo poweroff
```

With no known Wi-Fi and the radio on, `uav-wifi-fallback` brings up the **`UAV-Link`**
access point ~60 s after boot → the whole base setup is done over the AP + web UI.

## 3. Pull the image

Power off, take the SD to a PC, read a full image of it (Raspberry Pi Imager can back up
an SD, or `dd`/Win32DiskImager). Shrink/compress if you like (`.img.gz`/`.xz`/`.zst` —
Imager writes those directly). RPi OS expands the root partition to fill the target card
on first boot, so a same-or-larger SD works.

## Result

Users flash the image, boot, join the `UAV-Link` AP (`uavlink2026`), open
`http://10.42.0.1:8080`, and hit **Software Update** for the latest code — over Wi-Fi or,
once configured, LTE. No console, no install step.
