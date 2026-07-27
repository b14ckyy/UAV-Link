# UAV-Link

Custom **air unit** on a Raspberry Pi Zero 2W: low-latency analog (CVBS) video as
H.264 over 4G/LTE to a ground control station, plus a transparent MSP/MAVLink data
link to an INAV/ArduPilot flight controller — all over a single SIM, orchestrated
through a WireGuard VPN for stable IPs.

Everything is configured from a self-contained web UI on the Pi; no cloud, no app.

## Architecture

```
 FC (INAV/ArduPilot)          Raspberry Pi Zero 2W (air unit)              Ground
 ─────────────────            ────────────────────────────────            ──────
  MSP/MAVLink  ── UART/USB ──►  serial bridge ──┐
                                                ├─ WireGuard over LTE ──►  GCS
  CVBS ──► USB dongle ──► H.264 (HW) ── RTSP ───┘   (SIM7600 4G HAT)       (Kite/
                                                                            QGC/MP)
  Web UI (config) · OLED status · Wi-Fi/AP fallback · LTE hardening firewall
```

- **Video:** `v4l2src (MJPG) → jpegdec → v4l2h264enc (HW) → RTSP (UDP-only, zero-latency)`.
  ~200 ms glass-to-glass incl. the HDMI→CVBS converter; lower with a native analog cam.
- **Serial bridge:** protocol-agnostic core + MSP and MAVLink modules. **Transparent** —
  the GCS never sees the bridge's own traffic (MSP uses the ILMI flag to reclaim its
  replies; MAVLink stays passive + injects `RADIO_STATUS`). Injects LTE link stats as
  RC-link telemetry, disables Wi-Fi on arm (30 s delay). Python for now, hardened
  (fuzz-tested, systemd watchdog, auto-restart).
- **Network:** WireGuard tunnel pinned to the LTE interface; nftables hardens `wwan0`
  so only the tunnel is reachable over cellular (LAN/VPN stay open). Wi-Fi AP fallback
  when no known network appears.

Full tool stack, systemd wiring, ports, and data flows: [`SOFTWARE.md`](SOFTWARE.md).

## Hardware

Reference build: **Raspberry Pi Zero 2 W + Waveshare SIM7600G-H 4G HAT** + a USB CVBS→UVC
(MJPG) dongle. The cellular layer runs through ModemManager, so most 4G/5G modems work
without code changes. Tested vs. untested parts and modem-portability notes:
[`HARDWARE.md`](HARDWARE.md).

## Layout

| Path | What |
|------|------|
| `air-unit/` | the software running on the Pi (`~/uav-link/`) |
| `air-unit/*.py` | `rtsp-server`, `webui`, `msp-bridge`, `wifi-fallback`, `oled-display` |
| `air-unit/uav-wg-apply`, `reset-credentials.sh` | privileged helper + credential reset |
| `air-unit/uav-firewall.nft` | nftables ruleset (deploys to `/etc/nftables.conf`) |
| `air-unit/config.example.json` | runtime config (video / MSP / OLED) |
| `air-unit/test/` | loopback + fuzz test harness (MSP & MAVLink) |
| `systemd/` | the `uav-*.service` units |
| `system/` | sudoers, `config.txt` additions, WireGuard config template |

## Web UI

`http://<pi>:8080` — video pipeline, cellular (APN/PIN), Wi-Fi + AP + known networks,
WireGuard (paste/upload client config), MSP/MAVLink, OLED, system stats. IP-based auth
(default password `uavlink2026`, forced change on first connect). Reachable over LAN,
Wi-Fi AP, or the VPN.

## Safety & security

**Change the default passwords first** (web UI, Wi-Fi AP, and the `link` SSH account all
ship as `uavlink2026`). Over cellular, the firewall seals the raw LTE link — nothing is
reachable on the public IP, so **remote access requires the WireGuard VPN and can't be
bypassed**. For lowest latency, run the WireGuard server on the GCS itself. Full details:
[`SAFETY.md`](SAFETY.md).

## Status

Streaming, network, config center, serial bridge (MSP+MAVLink), firewall, hotspot
fallback, OLED, web-auth, the idempotent installer, the in-UI software updater
(Releases/Beta/Development), SD→USB backup, and a flashable golden image are all built.
Pending: field validation (real FC over MSP/MAVLink, video over LTE end-to-end). A native
C rewrite of the bridge was evaluated and **dropped** — Python is robust enough for this
off-the-flight-loop role.

Install: run [`install/install.sh`](install/README.md) on a fresh **Raspberry Pi OS Lite
(64-bit)**, or flash the prebuilt golden image and update from the web UI.

## Secrets

`webui-auth.json` (password hash) and the real `wgnet.conf` (private keys) are
git-ignored. Never commit them.
