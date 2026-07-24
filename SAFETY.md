# Safety & Security

UAV-Link puts a configurable device on a public cellular network. Read this before you fly
or expose a unit.

## 1. Change the default passwords — do this first

Three separate secrets all ship as **`uavlink2026`**:

| Secret | Change it via |
|--------|---------------|
| Web UI password | You're prompted on first connect (forced while default). |
| Wi-Fi access-point password | **Network** panel in the web UI. |
| `link` SSH account password | `passwd` over SSH — or disable password login and use keys. |

The web UI shows **red banners** while the UI or hotspot password is still the default.
Until you change them, **anyone within Wi-Fi range** (AP `UAV-Link` / `uavlink2026`) can
reach the config UI. Treat a default-password unit as open.

Web auth is IP-based (no cookies): a new client IP must re-authenticate, and access
re-locks after 10 min of no traffic. It works over LAN, the Wi-Fi AP, and the VPN.

## 2. Mandatory VPN over cellular — the raw LTE link is sealed

The nftables firewall (`air-unit/uav-firewall.nft`) locks down the cellular interface
`wwan0`: it **drops everything inbound except return traffic for connections the Pi itself
started**. The Pi dials *out* to your WireGuard server, so the tunnel's return packets are
allowed — nothing else is.

Consequences:

- The unit's public/CGNAT cellular IP exposes **nothing** inbound — no web UI, no SSH, no
  RTSP. **Knowing the IP does not let anyone in; the VPN cannot be bypassed over LTE.**
- All remote access (web UI, MSP/MAVLink, RTSP) rides **inside the WireGuard tunnel**.
  WireGuard's cryptokey routing means only peers holding your keys can talk to the unit.
- **Remote operation therefore requires WireGuard.** An LTE-only unit with no tunnel
  configured is intentionally unreachable from the outside.

Wi-Fi / LAN is deliberately **not** sealed, so you can reach the config UI directly on a
trusted bench/home network without the VPN. The security model assumes Wi-Fi is trusted —
**do not join a unit with default passwords to an untrusted Wi-Fi.**

## 3. WireGuard keys

- The real `wgnet.conf` (private keys) is **git-ignored — never commit it.**
- Give **each unit its own keypair**. Paste or upload the client config in the web UI.
- The tunnel endpoint is pinned to `wwan0`, so it always egresses over cellular even when
  Wi-Fi is up.

## 4. Wi-Fi safety net (availability, not a bypass)

- Wi-Fi is **forced ON at every boot** (runtime-disable only) so an LTE-only test can always
  be recovered.
- No known network within 60 s → the Pi raises the `UAV-Link` access point; or hold
  **GPIO21 / pin 40** for 3 s to raise it manually.
- Disabling Wi-Fi drops LAN access — continue over the VPN at `http://10.192.1.1:8080`.
- **Arm → Wi-Fi off:** Wi-Fi auto-disables ~30 s after the FC arms (config `arm_wifi_off`,
  default **off** for safe validation — logs the intent instead of cutting).

## 5. Latency considerations

Put the **WireGuard server on the GCS itself** for the lowest latency. The path is then:

```
air unit ──LTE──► WireGuard ──► GCS          (ideal: server co-located with the GCS)
```

A **relayed / off-site WireGuard host** (a cloud VPS, or a home server the GCS reaches
separately) adds a detour:

```
air unit ──LTE──► off-site host ──► GCS       (+20–50 ms RTT typical, sometimes more)
```

Depending on the host's location and your provider's routing, that off-site hop typically
adds **20–50 ms round-trip** — occasionally more on poor peering. Rule of thumb: the
endpoint the air unit dials should be as network-close to the GCS as possible; co-locating
the WireGuard server with the GCS is best.

Glass-to-glass is already ~200 ms through the HDMI→CVBS converter (lower with a native
analog camera) — don't stack avoidable VPN hops on top of it.
