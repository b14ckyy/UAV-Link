# Install

Two ways to install onto a Raspberry Pi (Zero 2W target). The installer is
**idempotent** — safe to re-run; it preserves your `config.json` and web/hotspot
credentials, adds boot-config edits only once, and skips work already done.

## A. Automatic — boot-partition drop-in (simplest, no console)

1. Flash stock Raspberry Pi OS with Raspberry Pi Imager; in the customization dialog
   set the user, **Wi-Fi with internet**, SSH and locale (the normal, no-file-editing way).
2. Copy [`firstboot.sh`](firstboot.sh) to the **boot partition** (the small FAT drive
   that appears after flashing). Keep the filename.
3. Append one token to the **end** of `cmdline.txt` on that same partition — it is a
   single line, add a leading space and **no** newline:
   ```
   systemd.run=/boot/firmware/firstboot.sh
   ```
4. Boot. It runs in two automatic stages (the `systemd.run` hook boots into a minimal
   target with no network, so stage 1 just schedules a proper networked installer and
   reboots; stage 2 installs during a normal boot and reboots again, fully configured).
   Runs exactly once. Progress log: `uav-setup.log` on the boot partition.

Edit `REF=` at the top of `firstboot.sh` to pin a release tag instead of `main` for
reproducible images. (A cloud-init variant is in [`user-data.example`](user-data.example).)

## B. Manual (existing system / testing)

```bash
git clone https://github.com/b14ckyy/UAV-Link
sudo bash UAV-Link/install/install.sh
sudo reboot
```

or from a release tarball:

```bash
curl -fsSL https://github.com/b14ckyy/UAV-Link/archive/refs/tags/<tag>.tar.gz | tar -xz
sudo bash UAV-Link-<tag>/install/install.sh
sudo reboot
```

## What it does

Installs packages (GStreamer, ModemManager, NetworkManager, nftables, WireGuard,
Flask, luma.oled venv), deploys the code to `~/uav-link`, fills the systemd/sudoers
placeholders with the detected user, enables all `uav-*` services + the LTE firewall,
adds the UART/I2C/dwc2 overlays to `config.txt` and frees the serial console, creates
an empty-APN WWAN profile, and disables cloud-init (first-boot provisioning done).

## After install

Reboot, then open `http://<pi>:8080` — default web password `uavlink2026` (you are
prompted to change it). Configure APN, WireGuard (paste your client config), camera,
MSP/MAVLink, and OLED from there. See the repo `README.md` and `system/README.md`.
