# System integration files

These are **install-time artifacts**. The Python/shell code derives its own paths at
runtime (from `__file__`), so it carries no username — but systemd units and sudoers
must name a concrete user and path. Those use two placeholders the installer fills in
with whatever username the operator chose when imaging:

| Placeholder | Substitute with | Example |
|-------------|-----------------|---------|
| `__UAVLINK_USER__` | the primary login user | `pi`, `marc`, … |
| `__UAVLINK_DIR__` | the install directory | `/home/<user>/uav-link` |

The installer does a simple substitution, e.g.:

```sh
USER=$(id -un 1000)                     # first non-system user
DIR="$(getent passwd "$USER" | cut -d: -f6)/uav-link"
for f in systemd/*.service system/sudoers.d-uav-web; do
  sed "s#__UAVLINK_USER__#$USER#g; s#__UAVLINK_DIR__#$DIR#g" "$f" > /tmp/out && install ...
done
```

## Placement

| File | Destination |
|------|-------------|
| `systemd/uav-*.service` | `/etc/systemd/system/` (then `systemctl enable --now`) |
| `system/sudoers.d-uav-web` | `/etc/sudoers.d/uav-web` (mode 0440, `visudo -c`) |
| `air-unit/uav-firewall.nft` | `/etc/nftables.conf` (`systemctl enable nftables`) |
| `system/config.txt-additions.md` | applied to `/boot/firmware/config.txt` |

## Service user requirements

The `__UAVLINK_USER__` must be in these groups for the services to work:
`video`, `render` (HW encoder), `dialout` (FC UART/USB), `netdev`, `gpio`, `i2c`, `spi`.
`uav-msp`, `uav-oled`, `uav-wifi-fallback`, `uav-wifi-on` run as root; only
`uav-rtsp` and `uav-web` run as the user (hence the sudoers rules).
