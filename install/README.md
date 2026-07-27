# Install

The installer is **idempotent** — safe to re-run; it preserves your `config.json` and
web/hotspot credentials, adds boot-config edits only once, and skips work already done.
That same property powers the **Update** button in the web UI (it just re-runs this).

## Manual install (your own Pi / any Debian-based image)

**Use Raspberry Pi OS Lite (64-bit).** In Raspberry Pi Imager it sits under
*Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)** — not the Desktop or Full
variant. The air unit is headless and is operated entirely through the web UI, so a
desktop stack is several GB of card space and a pile of background services that never
get used. 64-bit is required: the code is built and tested on `aarch64`.

Desktop images do work if you already have one — nothing in the installer depends on
Lite — but plan for a larger card (the 8 GB minimum in
[`HARDWARE.md`](../HARDWARE.md) assumes Lite).

```bash
curl -fsSL https://github.com/b14ckyy/UAV-Link/archive/refs/heads/main.tar.gz | tar -xz
sudo bash UAV-Link-main/install/install.sh
sudo reboot
```

or from a release tag:

```bash
curl -fsSL https://github.com/b14ckyy/UAV-Link/archive/refs/tags/<tag>.tar.gz | tar -xz
sudo bash UAV-Link-<tag>/install/install.sh
sudo reboot
```

Then open `http://<pi>:8080` — default password `uavlink2026` (you're prompted to change
it). Configure APN, WireGuard, camera, MSP/MAVLink, OLED from there.

## Ready-made image (recommended for most users)

Flash the pre-built **Vanilla image** to an SD card with Raspberry Pi Imager, boot, and
the Pi comes up as an access point (`UAV-Link` / `uavlink2026`). Connect, open the web UI,
and hit **Software Update** to pull the latest version — no console, no install step.
Building that image: see [`make-golden-image.md`](make-golden-image.md).

## What the installer does

Installs packages (GStreamer, ModemManager, NetworkManager, nftables, WireGuard, Flask,
luma.oled venv), deploys the code to `~/uav-link`, fills the systemd/sudoers placeholders
with the detected user, enables all `uav-*` services + the LTE firewall, adds the
UART/I2C/dwc2 overlays to `config.txt`, creates an empty-APN WWAN profile, records the
version, and disables cloud-init.

## Update channels (web UI)

| Channel | Source |
|---------|--------|
| Releases | latest **stable** GitHub Release (pre-releases excluded) |
| Beta | newest GitHub Release, **including** pre-releases |
| Development | the `main` branch |

Releases/Beta require published GitHub **Releases** (not just tags); mark beta ones as
"pre-release". Development always works.
