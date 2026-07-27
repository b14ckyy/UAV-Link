# Building the Vanilla / golden image

A clean, personal-data-free image users can flash and update from the web UI. Set it up
once on a Pi, strip identifying/stale state, pull a disk image.

## 1. Base install (over LAN — no Wi-Fi credentials get baked in)

- Flash stock **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager (under *Raspberry
  Pi OS (other)*). Lite keeps the image small enough to publish and to fit an 8 GB card —
  a Desktop base would bloat the release for no benefit on a headless unit. In the
  customization dialog set:
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

**Easiest — built-in (web UI → System Backup):** do the strip steps in section 2 but
**skip the `poweroff`**. Plug a USB stick (exFAT/ext4 for cards >4 GB — FAT32 caps files
at 4 GiB), open the web UI, and hit **Create backup to USB**. It writes a compressed
`uav-link-backup-*.img.gz` of the whole card straight onto the stick (~10–30 min). That
file *is* the golden image. (Reads the live card, so run it right after the strip commands
and don't touch the box meanwhile.)

**Offline alternative:** `poweroff`, take the SD to a PC, and read a full image with
Raspberry Pi Imager (SD backup) or `dd`/Win32DiskImager.

## 4. Re-arm first-boot expansion (required) + shrink

RPi OS grows the root filesystem to fill the card only on a **fresh flash's first boot**,
via a one-shot `init=` entry in `cmdline.txt` that removes itself after running. This Pi
already consumed that during setup, so the golden image would **not** expand on the user's
card — it would stay at this source card's size.

Re-arm it (and shrink the image so it downloads/flashes small) with
[PiShrink](https://github.com/Drewsif/PiShrink). It's a block-level tool (loop device +
`resize2fs`), so it needs the **fully decompressed** `.img` — plan for it:

- **Target filesystem must be exFAT or ext4, not FAT32.** The unpacked `.img` equals the
  source card size (e.g. 8 GB) and blows past FAT32's 4 GiB file limit. Check with
  `lsblk -f`. Need ~2x the unpacked size free (unpacked image + repack).
- **On the Pi, compress with `-z` (gzip), not `-Z` (xz).** xz is painfully slow on the
  Zero 2W's CPU; gzip is far faster and still fits under the 2 GiB GitHub-Release limit.
  Use `-Z` only when shrinking on a fast PC.

```bash
cd /path/to/usb-stick
gunzip uav-link-backup-*.img.gz                 # -> full-size .img (e.g. 8 GB)
curl -fsSL https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh -o pishrink.sh
chmod +x pishrink.sh
sudo apt-get install -y parted                  # e2fsprogs is already present
sudo ./pishrink.sh -z uav-link-backup-*.img     # shrink to used size + re-arm expand + gzip
```

Only the `.img` on the stick is touched — the live SD is never involved. Over USB 2.0 on
a Zero 2W budget ~30–45 min (mostly `resize2fs` random I/O). PiShrink injects its own
expand-on-first-boot service (independent of `cmdline.txt`) and re-enables it by default
— do **not** pass `-s` (that disables it). The result flashes small and grows to fill any
card **>= the source card** on the user's first boot. (No PiShrink? The image still boots;
users run `sudo raspi-config --expand-rootfs` once to reclaim the rest.)

## Result

Users flash the image, boot, join the `UAV-Link` AP (`uavlink2026`), open
`http://10.42.0.1:8080`, and hit **Software Update** for the latest code — over Wi-Fi or,
once configured, LTE. No console, no install step.
