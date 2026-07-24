# Install

Two ways to install onto a Raspberry Pi (Zero 2W target). The installer is
**idempotent** — safe to re-run; it preserves your `config.json` and web/hotspot
credentials, adds boot-config edits only once, and skips work already done.

## A. Automatic (fresh image, first boot)

Flash stock Raspberry Pi OS with Raspberry Pi Imager, set hostname / user / **Wi-Fi
with internet** / SSH / locale in the customization dialog, then add the bootstrap
from [`user-data.example`](user-data.example) to the boot-partition `user-data`,
pinning a release tag that contains `install/install.sh`. On first boot the Pi
provisions itself over Wi-Fi and reboots fully configured (log:
`/boot/firmware/uav-setup.log`).

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
