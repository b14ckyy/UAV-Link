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
| Video capture | **USB CVBS→UVC dongle (MJPG)** | Any UVC dongle that outputs MJPG — but read the bandwidth section below before buying. |
| Video capture | **MacroSilicon HDMI→UVC dongle (534d:2109, MJPG)** | Sustains native 720p60 MJPEG (~42 Mbit/s isochronous) cleanly. |
| Status display *(optional)* | **128×64 I²C OLED** — SSD1306 (0.96") or SH1106 (1.3") | Controller + address selectable in the web UI. |

## USB capture bandwidth on the Pi Zero 2 W — read this before buying a capture device

The Zero 2 W's USB controller (`dwc_otg`) limits **isochronous** transfers — the mode every
USB video device uses — well below the bus rate. The ceiling is **per device**: it depends
on the isochronous endpoint the device offers and `uvcvideo` selects, not on the platform
as a whole. Measured with an 800-byte-endpoint CVBS dongle:

| Requested | Delivered | Data rate |
|-----------|-----------|-----------|
| 1280x720 @60 | 51–55 fps (85–92 %) | 34–38 Mbit/s |
| 1280x720 @50 | 48 fps (96 %) | 33 Mbit/s |
| 1280x720 @30 | **60 fps → 100 %** | 19–21 Mbit/s |
| 720x576 @60 | **60 fps → 100 %** | 21–25 Mbit/s |
| 720x576 @50 | **50 fps → 100 %** | 19–21 Mbit/s |
| 720x480 @60 | **60 fps → 100 %** | 22 Mbit/s |

**For this device class the limit is the data rate, not the frame rate.** Everything up to
roughly **22 Mbit/s** arrives complete, including 60 fps. Above ~33 Mbit/s frames are
silently dropped, and those missing frames are what shows up as stutter.

This is not the bus being full: bulk transfers on the same controller and hub reach
**248 Mbit/s read / 236 Mbit/s write** (measured against a USB SSD). Isochronous transfers
are scheduled differently — a reserved slice per microframe, with no retries. Stress-tested
(16.08.2026): a 720p60 MJPEG stream at ~42 Mbit/s isochronous ran through **two chained
hubs** (HAT hub + external hub) while a full SD-card image backup was writing to a USB
stick on the same chain and the flight controller sat next to it — the capture never
collapsed (54 fps sustained; the small dip was CPU from the backup's gzip, not USB). Hubs
and bus have ample headroom; the reserved-slice scheduling does its job.

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

A device with a **full-size single-transaction endpoint (1x 1024 B, ~65 Mbit/s budget)**
clears the bar comfortably: the MacroSilicon HDMI→UVC dongle delivers native
**720p60 MJPEG at 42.4 Mbit/s with zero drops** on the same controller. So check the
endpoint descriptors (`lsusb -v`) before buying — the packet size of the alt setting
`uvcvideo` will pick *is* the ceiling for that device.

> **Never set `dtoverlay=dwc2` on the Zero 2 W.** It swaps the stock `dwc_otg` host driver
> (FIQ-assisted isochronous scheduling) for the upstream `dwc2` driver, whose isochronous
> scheduling cannot carry UVC loads: 720p60 fails outright, 720p30 is marginal. The stock
> driver already runs the port in host mode — the overlay is never needed, and because it
> only takes effect at boot it produces bafflingly delayed breakage.

**There is no way to force a faster mode from userspace.** Verified as ineffective:
`uvcvideo quirks=0`, `quirks=128` (FIX_BANDWIDTH), `hwtimestamps=1`; the `dwc_otg`
parameters are already at their most capable (`fiq_fsm_enable=Y`, `fiq_fsm_mask=15`,
`microframe_schedule=Y`). Raising the ceiling would mean driver work.

Cross-platform check with the **same 800-B-endpoint CVBS dongle**: flawless 720p60 on a
Windows PC and on a **Pi 5 running the same Raspberry Pi OS** — but the **same cap on a
Dell laptop running Debian** despite its Intel 10-Gbit USB 3.1 xHCI controller. So the
ceiling is not the host controller alone and not the device alone: it is how the Linux
driver stack negotiates the isochronous alt setting on a given host (Windows simply drives
the same hardware at full rate).

### What follows for device choice

- **Capture at the source's native resolution.** A CVBS signal carries 480 or 576 lines;
  letting the dongle upscale to 720p triples the data rate without adding any picture
  information, and that is exactly what breaks the budget. 720x576 @50 (PAL) and
  720x480 @60 (NTSC) both deliver **100 %** of frames.
- **Native 720p60 MJPEG works only with the right endpoint.** It needs 35–45 Mbit/s, which
  a 1024-B-endpoint device (e.g. MacroSilicon 534d:2109) sustains cleanly — the common
  800-B alt-1 class does not.
- **Prefer capture devices with their own H.264 encoder.** They need only 5–10 Mbit/s, fit
  comfortably, and additionally save the Pi the whole decode-and-re-encode step.
- **A stronger Pi removes the limit — validated.** The same dongle delivers clean 720p60
  on a **Pi 5** under the same OS (xHCI handles high-bandwidth isochronous endpoints
  normally). Note this is not a blanket xHCI guarantee: a Debian laptop with an Intel
  xHCI showed the same cap (see above).

## HDMI capture over CSI — the way around the USB ceiling

A **HDMI→CSI-2 bridge** on the camera connector sidesteps the isochronous limit entirely:
it does not touch USB, and it removes the analog detour (digital → CVBS → digital) that
costs both latency and picture quality. Supported chip: **Toshiba TC358743**.

Two things about this chip decide whether it works at all, and UAV-Link handles both
automatically (`air-unit/uav-hdmi-setup`, run at boot by `uav-hdmi.service` and again by
the RTSP server whenever no usable source is found):

1. **It ships without an EDID.** The HDMI source asks for one, gets nothing, and therefore
   sends nothing — the classic "everything is wired correctly and the picture is still
   black". The EDID also decides which modes the source will offer at all.
2. **DV timings must be locked to the incoming signal.** Until that happens the driver
   reports 0×0 and every capture attempt fails immediately.

**The chip tops out at a 165 MHz pixel clock — 720p60 *or* 1080p30, never 1080p60.**
The built-in EDID (`v4l2-ctl --set-edid=type=hdmi`) advertises up to 1080p60, which is
fine for a source you set manually. A source that always picks the highest advertised mode
needs a restricted EDID: drop a file at `/boot/firmware/tc358743.edid` and the helper uses
it instead (it survives updates).

Resolution and frame rate are **not settings** on this path — they follow the signal. The
web UI shows them read-only, and the RTSP server adopts whatever is actually arriving.
MJPEG "source quality" (passthrough) does not exist for CSI either: there is no JPEG at the
source, so the server falls back to H.264 and says so in the log.

Pipeline-wise this is the cheapest path available on a Zero 2 W: both `v4l2h264enc` and
`v4l2jpegenc` accept the bridge's `UYVY` frames directly, so there is **no JPEG decode and
no colour-space conversion** — neither `videoconvert` (CPU) nor `v4l2convert` (ISP).

Bandwidth is not a concern: 720p60 in UYVY is ~885 Mbit/s and 1080p30 ~995 Mbit/s over
CSI-2, comfortably within the Zero 2 W's two lanes.

> **Status: implemented, not yet verified against hardware.** The code, the installer wiring
> and the boot-time setup are in place; the first real capture is still pending.

**Trade-off:** the installer adds `dtoverlay=tc358743` and sets `camera_auto_detect=0`
unconditionally — harmless on a Pi without the HAT (the driver simply finds nothing to bind
to), but it does claim the CSI connector, so an official Pi camera will not be picked up.
UAV-Link does not support those anyway.

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
