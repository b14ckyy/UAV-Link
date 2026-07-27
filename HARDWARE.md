# Supported Hardware

The cellular layer runs through **ModemManager + NetworkManager**, so UAV-Link is broadly
modem-agnostic: most 4G/5G modems ModemManager supports will connect and stream with **no
code changes**. Only two things carry model assumptions — the cellular interface name
(`wwan0`) and the detailed signal metrics (QMI via `qmicli`). See
[Modem portability](#how-the-portability-works-for-tinkerers) at the bottom.

## Validated (tested)

| Role | Part | Notes |
|------|------|-------|
| SBC | **Raspberry Pi Zero 2 W** | Reference platform. Quad-core — enough for HW H.264 + the Python orchestrator. |
| Cellular | **Waveshare SIM7600G-H 4G HAT** | Qualcomm-based; QMI on `/dev/cdc-wdm0`, enumerates as `wwan0`. Global bands. |
| Video capture | **USB CVBS→UVC dongle (MJPG)** | Any UVC dongle that outputs MJPG. Needs a USB host port (OTG / `dwc2` on the Zero). |
| Status display *(optional)* | **128×64 I²C OLED** — SSD1306 (0.96") or SH1106 (1.3") | Controller + address selectable in the web UI. |

## USB capture bandwidth on the Pi Zero 2 W — read this before buying a capture device

The Zero 2 W's USB controller (`dwc_otg`) caps **isochronous** transfers — the mode every
USB video device uses — far below the bus rate. Measured on hardware:

| Requested | Delivered | Data rate |
|-----------|-----------|-----------|
| 1280x720 @60 | 51–55 fps (85–92 %) | 34–38 Mbit/s |
| 1280x720 @50 | 48 fps (96 %) | 33 Mbit/s |
| 1280x720 @30 | **60 fps → 100 %** | 19–21 Mbit/s |
| 720x576 @60 | **60 fps → 100 %** | 21–25 Mbit/s |
| 720x576 @50 | **50 fps → 100 %** | 19–21 Mbit/s |
| 720x480 @60 | **60 fps → 100 %** | 22 Mbit/s |

**The limit is the data rate, not the frame rate.** Everything up to roughly **22 Mbit/s**
arrives complete, including 60 fps. Above ~33 Mbit/s frames are silently dropped, and those
missing frames are what shows up as stutter.

This is not the bus being full: bulk transfers on the same controller and hub reach
**248 Mbit/s read / 236 Mbit/s write** (measured against a USB SSD). Isochronous transfers
are scheduled differently — a reserved slice per microframe, with no retries.

The cause is the alternate setting the driver ends up using. The capture device offers
three isochronous modes, and `uvcvideo` consistently selects the slowest:

| Alt setting | Packet size | Theoretical | Selected |
|---|---|---|---|
| 1 | 1x 800 B | 51 Mbit/s | **yes** |
| 2 | 2x 1024 B | 131 Mbit/s | no |
| 3 | 3x 1024 B | 197 Mbit/s | no |

Alt 2 and 3 are *high-bandwidth* endpoints (several transactions per microframe), which
`dwc_otg` does not appear to support — its isochronous handling is FIQ-assisted in software.
Confirmed by reading the active alt setting during a live stream.

**There is no way to force a faster mode from userspace.** Verified as ineffective:
`uvcvideo quirks=0`, `quirks=128` (FIX_BANDWIDTH), `hwtimestamps=1`; the `dwc_otg`
parameters are already at their most capable (`fiq_fsm_enable=Y`, `fiq_fsm_mask=15`,
`microframe_schedule=Y`). Raising the ceiling would mean driver work.

### What follows for device choice

- **Capture at the source's native resolution.** A CVBS signal carries 480 or 576 lines;
  letting the dongle upscale to 720p triples the data rate without adding any picture
  information, and that is exactly what breaks the budget. 720x576 @50 (PAL) and
  720x480 @60 (NTSC) both deliver **100 %** of frames.
- **Native 720p60 MJPEG will not work on a Zero 2 W** — any such device needs 35–45 Mbit/s.
- **Prefer capture devices with their own H.264 encoder.** They need only 5–10 Mbit/s, fit
  comfortably, and additionally save the Pi the whole decode-and-re-encode step.
- **A stronger Pi removes the limit.** Pi 4, Pi 5, CM4 and CM5 use xHCI controllers, which
  handle high-bandwidth isochronous endpoints normally.

## Should work — untested

### Other Raspberry Pi models

- **Anything ≥ Pi Zero 2 W**: Pi 3, Pi 4, Pi 5 — same software (all aarch64), just more
  CPU/thermal headroom. Overkill for one stream, but fine.
- **Compute Module CM4 / CM5** on a carrier board that exposes USB (modem + capture) and,
  if wanted, I²C/UART. Same software.
- A Pi with a **native MIPI/analog camera** could drop the USB CVBS dongle entirely (lower
  latency) — the GStreamer pipeline would need a source swap.

### Other cellular modems (USB or HAT)

Any ModemManager-supported modem connects via the generic `gsm` profile. For full parity
(RSRP/RSRQ/SNR + band readout) the modem should speak **QMI on `/dev/cdc-wdm0`** and
enumerate as **`wwan0`** — which most Qualcomm-based HATs do:

- **Quectel** EC25 / EG25-G / EM06 (LTE), RM500Q-GL / RM502Q-AE (5G)
- **SIMCom** SIM7600 variants (SIM7600E-H, A7600, …), SIM8200 (5G)
- Other **Waveshare / Sixfab** HATs built around the above
- USB LTE sticks in **stick/QMI mode** (many Quectel-based dongles)

Caveats:

- **HiLink / RNDIS dongles** (e.g. Huawei E3372 in HiLink mode) present as a virtual
  ethernet with their own NAT — they bypass ModemManager and the `wwan0` firewall/routing
  model. Switch such a dongle to **stick/QMI mode**, or expect to adapt the interface
  handling.
- **MBIM-only modems** connect fine, but the detailed signal metrics (via `qmicli`) come up
  empty — the basic signal % (ModemManager) still works. A `mbimcli` path would be needed
  for parity.
- **5G modems** connect and stream fine; the band / "3G/4G/5G" readout parses LTE
  (`eutran-…`) and needs a small tweak for 5G NR (`nr5g-…`).

### Displays

Any **luma.oled**-supported I²C panel. SSD1306 and SH1106 are exposed in the UI (address
`0x3C` / `0x3D`).

## Not supported

- **Raspberry Pi Zero 1 / Zero W** — single-core ARMv6: too weak for HW-encode + the
  orchestrator, and ARMv6 has poor package/architecture support. **Won't work.**
- Modems offering **only HiLink/RNDIS** with no switchable QMI/serial mode — they don't fit
  the integration model (see the modem caveat above).

## Power & storage

- **Supply: 5 V, 3 A+ recommended.** LTE TX bursts plus the Pi and the capture dongle can
  spike current; an undersized supply causes brownouts (CPU throttling, dropped USB video
  frames). A 2–3 A BEC works on-vehicle.
- **microSD: 8 GB minimum, 16 GB+ recommended.** The golden image auto-expands to fill the
  card on first boot. The 8 GB figure assumes **Raspberry Pi OS Lite (64-bit)** — pick a
  larger card if you install onto a Desktop image.

## Operating system

**Raspberry Pi OS Lite, 64-bit (`aarch64`).** In Raspberry Pi Imager: *Raspberry Pi OS
(other)* → *Raspberry Pi OS Lite (64-bit)*. The air unit runs headless and is operated
through the web UI, so the desktop stack only costs card space and background services.
64-bit is not optional — the whole stack is built and tested on `aarch64`, and the
unsupported Pi Zero 1 / Zero W fail on exactly that point (ARMv6).

## How the portability works (for tinkerers)

Connectivity and the basic signal value are model-agnostic — they go through ModemManager
(`mmcli -m a …`) and NetworkManager (`nmcli connection add type gsm`). The only
model-specific touchpoints in the code:

1. **Interface name `wwan0`** — hardcoded in the firewall (`air-unit/uav-firewall.nft`), the
   WireGuard route pin (`air-unit/uav-wg-apply`), and the throughput stats
   (`air-unit/webui.py`, `oled-display.py`, `msp-bridge.py`). Correct for typical WWAN HATs;
   change it in ~5 spots (or auto-detect) if your modem enumerates differently.
2. **Detailed signal via `qmicli -d /dev/cdc-wdm0`** (RSRP/RSRQ/SNR, band) in `webui.py`,
   `oled-display.py`, `msp-bridge.py`. Qualcomm/QMI-specific; works on most HATs and degrades
   gracefully (the basic signal % is still shown) on non-QMI modems.
