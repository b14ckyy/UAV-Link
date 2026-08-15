# UAV-Link OSD Downlink Protocol (v1)

The air unit parses MSP DisplayPort from the flight controller and offers
the resulting character grid to ground stations as a lightweight UDP
stream. A GCS renders the OSD locally with a glyph font — independent of
the video stream, so the OSD keeps updating even when video drops.

Design goals:

- **Stateless**: every packet is a complete snapshot (or an independent
  row stripe). Packet loss needs no recovery — the next draw replaces
  everything.
- **Firmware-agnostic**: the air unit hides MSP/DisplayPort semantics
  (fontpage attribute bits, canvas options). The GCS only ever sees
  glyph indices on a grid.
- **NAT/VPN friendly**: the GCS initiates; the unit replies to the
  observed source address. Works through WireGuard without extra
  configuration.

## Transport

- UDP port **5762** on the air unit — **fixed, part of this API**.
  The port is reserved: the web UI refuses it (and 5761, the internal
  loopback port) for the configurable serial tunnel.
- The downlink is always active; it only transmits to subscribers.

## Subscribing (GCS → unit, port 5762)

```
offset  size  content
0       4     magic 'OSUB' (0x4F 0x53 0x55 0x42)
4       1     version, currently 1
```

Send once to subscribe and repeat as keepalive at **1 Hz**. A subscriber
that stays silent for 5 s is dropped. Any number of subscribers may be
active at once. Extra bytes after the version are reserved and ignored.

## Grid snapshots (unit → GCS, to the subscriber's source address)

Sent whenever a DisplayPort `draw` **changed** the grid contents, plus
at 1 Hz as heartbeat regardless of changes. Judge link liveness by the
heartbeat, not by the packet rate: a static OSD legitimately produces
only ~1 packet/s. `status` bit0 tells whether the FC itself is alive.

Header (14 bytes, little-endian):

```
offset  size  content
0       4     magic 'UOSD'
4       1     version, currently 1
5       1     flags        bit0: payload is RLE-encoded (always 1)
6       1     rows         full grid height  (e.g. 20)
7       1     cols         full grid width   (e.g. 53)
8       2     seq (u16)    increments per send, wraps; equal for both
                           stripes of one split snapshot
10      1     row0         first row this packet covers
11      1     nrows        number of rows this packet covers
12      1     status       bit0: FC delivered a draw within the last 3 s
13      1     reserved (0)
```

Payload: the cells of rows `row0 .. row0+nrows-1` in row-major order,
RLE-encoded as a sequence of little-endian u16 tokens:

- `0xFFFF` is an escape: the **next** u16 is a count of consecutive
  empty (0) cells.
- any other value is one literal cell: glyph index `0..511`
  (character code + 256 × fontpage; fontpage 1 holds the colored
  variants in SneakyFPV-layout fonts). `0` never appears literally.

Decoding ends when `nrows × cols` cells are filled. A decoder MUST
ignore packets with unknown magic/version and SHOULD ignore a packet
whose payload decodes to a wrong cell count.

Normally one packet carries the whole grid (`row0=0, nrows=rows`; a
typical INAV screen is 400–800 bytes). If a very dense grid would
exceed ~1400 bytes (WireGuard MTU), the snapshot arrives as two row
stripes with the same `seq`. Apply each stripe as "replace rows
`row0..row0+nrows-1`" — the same logic handles both cases.

Grid sizes follow the FC's canvas: HD 53×20 (INAV "AVATAR"/HDZero
class) or SD 30×16. `rows`/`cols` may change at runtime; the GCS should
resize its canvas when they do.

## Rendering

Fonts are SneakyFPV-layout PNGs: 2 glyph columns × 256 rows = 512
glyphs, glyph index = row + 256 × column. The GCS may use any font it
likes. **Optionally** it can fetch the font currently uploaded to the
unit via HTTP:

```
GET http://<unit>:8080/osd_font     → image/png (404 if none uploaded)
```

Recommended client behavior (as implemented in the air unit's burn-in
renderer): center the grid on the video, cell aspect ≈ 2:3, clamp the
cell width so `cols × cell_w` fits the canvas.

## Bandwidth

Measured with a real INAV screen (53×20, ~500 bytes per snapshot):
sending every draw at 46 Hz would cost ~190 kbit/s; with change
detection a typical OSD (values updating ~1×/s) settles around
**4–40 kbit/s**. Worst case (dense grid changing on every draw):
~200 kbit/s — still negligible next to the video stream.
