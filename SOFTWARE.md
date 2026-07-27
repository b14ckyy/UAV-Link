# Software Stack & Wiring

What runs on the air unit, what it depends on, and how the pieces connect. All of it lives
in `~/uav-link/` (deployed from `air-unit/`) and is driven by a handful of `systemd` units.

## Layers

```
   ┌─ our code (Python + shell), deployed to ~/uav-link/ ─────────────────────┐
   │  rtsp-server · webui · msp-bridge · wifi-fallback · oled-display          │
   │  uav-update · uav-backup · uav-wg-apply · reset-credentials · firewall    │
   └──────────────────────────────────────────────────────────────────────────┘
                              │ drives / shells out to
   ┌─ system services & CLIs ─────────────────────────────────────────────────┐
   │  GStreamer · ModemManager (mmcli) · libqmi (qmicli) · NetworkManager      │
   │  (nmcli) · WireGuard · nftables · Flask · luma.oled · gpiozero/lgpio       │
   └──────────────────────────────────────────────────────────────────────────┘
                              │ on
   ┌─ Raspberry Pi OS Lite (Debian, 64-bit / aarch64) + systemd ──────────────┐
   └──────────────────────────────────────────────────────────────────────────┘
```

## Our components

| File (`air-unit/`) | systemd unit | Role |
|--------------------|--------------|------|
| `rtsp-server.py` | `uav-rtsp` | Builds the GStreamer pipeline and serves it over RTSP. |
| `webui.py` | `uav-web` | Flask config UI on `:8080`; orchestrates everything via CLIs. |
| `msp-bridge.py` | `uav-msp` | Transparent MSP/MAVLink bridge, FC ⇄ GCS; injects LTE link stats. |
| `wifi-fallback.py` | `uav-wifi-fallback` | Access-point fallback + GPIO button. |
| `oled-display.py` | `uav-oled` | I²C status display (runs in its own venv). |
| `uav-update` | `uav-update@<ch>` | Self-update: fetch a channel and re-run the installer. |
| `uav-backup` | `uav-backup` | Image the SD to a USB stick. |
| `uav-wg-apply` | *(sudo helper)* | Apply a pasted WireGuard config; pin the endpoint route to `wwan0`. |
| `reset-credentials.sh` | *(helper)* | Reset the web-UI auth back to default. |
| `uav-firewall.nft` | `nftables` | Ruleset deployed to `/etc/nftables.conf` (LTE hardening). |
| `config.example.json` | — | Template for the runtime `config.json`. |

## System tools & libraries

Installed by `install/install.sh` (apt), plus one pip venv:

| Function | Tools / packages |
|----------|------------------|
| Video capture + encode + serve | **GStreamer** (`gstreamer1.0-tools`, `-plugins-base/good/bad`, `-libav`, `-rtsp`, `gir1.2-gst-rtsp-server-1.0`, `python3-gi`); HW encoder `v4l2h264enc` |
| Video device enumeration | `v4l-utils` (`v4l2-ctl`) — the UI probes formats/resolutions |
| Web UI | **Flask** (`python3-flask`), stdlib only otherwise |
| Cellular modem | **ModemManager** (`mmcli`) for connect + generic signal; **libqmi** (`qmicli`) for RSRP/RSRQ/SNR + band |
| Networking / Wi-Fi / WWAN profile | **NetworkManager** (`nmcli`) — Wi-Fi client, hotspot, the `uav-wwan` GSM profile |
| VPN | **WireGuard** (`wireguard-tools`: `wg`, `wg-quick`) |
| Firewall | **nftables** |
| Status display | **luma.oled** (pip, in `~/uav-link/oled-venv/`) over I²C |
| GPIO button | `python3-gpiozero` + `python3-lgpio` |
| Test harness | `socat` (PTY loopback for the bridge tests) |

## systemd services

| Unit | Type | After | Restart | Notes |
|------|------|-------|---------|-------|
| `uav-rtsp` | simple | `network.target` | always | `KillSignal=SIGINT`, `TimeoutStopSec=15` — never hard-kill the bcm2835 codec |
| `uav-web` | simple | `network.target` | always | runs as the install user |
| `uav-msp` | **notify** | `ModemManager` | always | `WatchdogSec=20` (sd_notify), `Nice=-10`, `StartLimitIntervalSec=0` |
| `uav-oled` | simple | `uav-web`, `ModemManager` | always | `Nice=5`, venv Python |
| `uav-wifi-on` | oneshot | `NetworkManager` | — | forces the Wi-Fi radio ON at boot (retries) |
| `uav-wifi-fallback` | simple | `NetworkManager`, `uav-wifi-on` | always | AP after 60 s + GPIO button |
| `uav-update@<ch>` | oneshot | `network-online.target` | — | started on demand from the UI |
| `uav-backup` | oneshot | — | — | started on demand from the UI |

Plus the stock services we lean on: **ModemManager**, **NetworkManager**, **nftables**, and
`wg-quick@wgnet` (the tunnel).

## Wiring / data flows

### Video

```
CVBS ──► USB dongle ──► v4l2src (MJPG) ──► jpegdec ──► v4l2h264enc (HW, VBR/CBR)
                                                         └► h264parse ► rtph264pay ► RTSP
```

`rtsp-server.py` serves `rtsp://0.0.0.0:<port><mount>` (default `:8554/cam`), **UDP-only for
zero latency**. `set_shared(True)` — the dongle can be opened only once, so all clients share
one pipeline. Resolution/bitrate/rate-control come from `config.json` and can be changed live
(the pipeline restarts). The web UI's live preview grabs JPEG frames off the same stream.

A **session reaper** (short session timeout + periodic `session_pool.cleanup()`) removes
sessions of clients that vanished without a `TEARDOWN` — the normal case on a radio link.
Without it those zombie sessions live forever and keep blasting UDP at dead ports, which
drags the framerate down until the FPS watchdog restarts the *shared* media for everyone.

### Data link (MSP / MAVLink)

```
 FC  ◄── UART /dev/serial0  or  USB-VCP ──►  msp-bridge.py  ◄── UDP :5760 ──►  GCS
                                              │
                                              └─ injects LTE link stats toward the FC
                                                 (RC_LINK_STATS / RC_INFO), reads arm state
```

Protocol-agnostic core + MSP and MAVLink modules. **Transparent**: the GCS never sees the
bridge's own traffic (MSP reclaims its replies via the ILMI flag; MAVLink stays passive and
pushes `RADIO_STATUS`). A gap-filler status poll runs **only** when no GCS is connected.
On arm, Wi-Fi auto-disables after a delay (default off during validation). Runs under a
systemd watchdog and auto-restarts.

### Network & VPN

```
                 wlan0 (client or AP "UAV-Link")      ─ trusted, full access
   services  ──  eth0  (LAN, if present)              ─ trusted, full access
                 wgnet (decrypted WireGuard tunnel)   ─ trusted, full access
                 wwan0 (raw LTE)  ── nftables: only Pi-initiated return traffic
                                     (= the WireGuard tunnel). Everything else dropped.
```

The Pi dials **out** to the WireGuard server, so remote access rides inside the tunnel; the
raw cellular IP exposes nothing (see [`SAFETY.md`](SAFETY.md)). `uav-wg-apply` writes
`/etc/wireguard/wgnet.conf`, **enables** `wg-quick@wgnet` (so the tunnel returns after a
reboot) plus restarts it to apply the new config, and pins the endpoint's route to
`wwan0` so the tunnel always egresses over LTE. `wifi-fallback.py` raises the `UAV-Link` AP
when no known network appears within 60 s (or on a 3 s hold of GPIO21/pin 40).

### Config & privilege boundary

`webui.py`, `msp-bridge.py`, `oled-display.py` all read **`config.json`** (video + `msp` +
`oled` sections). The web UI writes it and restarts the affected service.

Services run as the **unprivileged install user**. Every privileged action goes through a
tightly-scoped `sudo` allow-list (`/etc/sudoers.d/uav-web`): `nmcli *`, `wg show *`,
`qmicli … --nas-get-signal-info`, `systemctl restart uav-{rtsp,msp,oled}`, `uav-wg-apply *`,
and `systemctl start --no-block uav-update@* / uav-backup.service`. The `uav-update@` and
`uav-backup` units themselves run as root (they mount, image, and re-run the installer).

## Ports & interfaces

| Port / iface | Proto | Used by |
|--------------|-------|---------|
| `:8080` | TCP | Web UI (Flask, `0.0.0.0`) |
| `:8554` (default) | RTSP/UDP | Video stream (`config.json` `port`/`mount`) |
| `:5760` (default) | UDP | MSP/MAVLink GCS side (`msp.udp_port`) |
| WireGuard endpoint | UDP | The VPN tunnel (from `wgnet.conf`) |
| `wwan0` | — | LTE data (NetworkManager `uav-wwan` GSM profile) |
| `wlan0` | — | Wi-Fi client / `uav-hotspot` AP |
| `wgnet` | — | WireGuard interface |

## Config & secret files (in `~/uav-link/` unless noted)

| File | Contents | In git? |
|------|----------|---------|
| `config.json` | video + `msp` + `oled` settings | no (per-unit; example is committed) |
| `VERSION` | JSON: channel / commit / dates (written by the installer) | no |
| `webui-auth.json` | web-UI password hash (pbkdf2) | **no — never commit** |
| `/etc/wireguard/wgnet.conf` | WireGuard keys | **no — never commit** |
| `/etc/nftables.conf` | deployed firewall ruleset | (from `uav-firewall.nft`) |
| `/etc/sudoers.d/uav-web` | the scoped sudo allow-list | (from `system/sudoers.d-uav-web`) |
