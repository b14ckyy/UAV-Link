# /boot/firmware/config.txt — UAV-Link additions

These lines are added to the stock Raspberry Pi OS `config.txt` (Pi Zero 2W).
Back up first (`config.txt.bak`). A reboot is required for changes to take effect.

```ini
# USB OTG host mode for the CVBS capture dongle (enumerates cleaner than dwc_otg)
dtoverlay=dwc2,dr_mode=host

# Hardware UART (PL011) on GPIO14/15 for the MSP bridge; Bluetooth disabled to free it
enable_uart=1
dtoverlay=disable-bt

# I2C for the OLED status display (SDA=GPIO2/pin3, SCL=GPIO3/pin5)
dtparam=i2c_arm=on
```

Also remove the serial console from `/boot/firmware/cmdline.txt` (otherwise a getty
grabs the UART and corrupts the MSP stream): delete `console=serial0,115200`.

`i2c_arm=on` can also be set via `sudo raspi-config nonint do_i2c 0`.
