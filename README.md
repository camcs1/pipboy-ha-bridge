# pipboy-ha-bridge

A Python bridge that connects a [Wand Company Pip-Boy 3000 Mk V](https://www.thewandcompany.com/fallout-pip-boy/) to [Home Assistant](https://www.home-assistant.io/) via MQTT, running on a Raspberry Pi Zero 2W.

The bridge communicates with the Pip-Boy over USB serial using the Espruino REPL, and exposes controls as MQTT entities that auto-discover in Home Assistant.

Serial commands are sourced from [CodyTolene/pip-terminal](https://github.com/CodyTolene/pip-terminal).

---

## Features

- **Power toggle** — sleep and wake the Pip-Boy from HA (ON = awake, OFF = sleeping)
- **Brightness slider** — 0–20 range, greyed out while sleeping
- **Demo mode toggle** — greyed out while sleeping
- **Torch toggle** — greyed out while sleeping
- **Battery sensor** — measured via ADC, polled every 60 seconds
- **Firmware sensor** — reports firmware version string on connect
- **Clock sync** — Pip-Boy clock synced to Pi system time on every connect
- **MQTT autodiscovery** — all entities appear automatically in HA under a single Pip-Boy 3000 MkV device, no YAML required

---

## Hardware

| Item | Notes |
|------|-------|
| Raspberry Pi Zero 2W | Built-in WiFi, no dongles needed |
| Micro USB power supply | 5V 2.5A into the PWR IN (left) port |
| USB-C to Micro USB cable | Data capable — connects Pip-Boy to the Pi's OTG (right) port |
| Micro USB OTG adapter | Micro USB male → USB-A female |
| MicroSD card | 8GB+ Class 10 |

The Pip-Boy connects to the Pi's **right** micro USB port (OTG/data). Power goes into the **left** micro USB port. The Pip-Boy uses its own battery — no charging via the Pi.

---

## Raspberry Pi Setup

### 1. Flash the SD card

Download and install [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

- **Device:** Raspberry Pi Zero 2W
- **OS:** Raspberry Pi OS Lite (64-bit)
- **Storage:** your SD card

Before writing, click **Edit Settings** and configure:

| Setting | Value |
|---------|-------|
| Hostname | `pipboy` |
| Username | your choice |
| Password | your choice |
| SSID | your WiFi network name |
| WiFi password | your WiFi password |
| Wireless LAN country | your country code |
| SSH | Enable, password authentication |

### 2. Edit boot files

Once written, the SD card will remount. Open the `bootfs` partition and make these edits.

**config.txt** — add to the `[all]` section at the bottom:
```
dtoverlay=dwc2
```

**cmdline.txt** — add `modules-load=dwc2,g_serial` immediately after `rootwait` on the single line (no line breaks):
```
... rootwait modules-load=dwc2,g_serial ...
```

Safely eject the SD card, insert into the Pi, and power on.

### 3. SSH in

```bash
ssh your-username@pipboy.local
```

If `pipboy.local` doesn't resolve, find the IP from your router's client list.

### 4. Update and install dependencies

```bash
sudo apt update && sudo apt upgrade -y
python3 -m venv ~/pipboy-env
~/pipboy-env/bin/pip install pyserial paho-mqtt
```

### 5. Verify the Pip-Boy is detected

Plug the Pip-Boy into the Pi's OTG port, then:

```bash
ls /dev/ttyACM*
```

You should see `/dev/ttyACM0`.

---

## Bridge Setup

### 1. Download the script

```bash
nano ~/pipboy_bridge.py
```

Paste the contents of `pipboy_bridge.py` and update the configuration at the top:

```python
MQTT_HOST = "your-ha-ip"
MQTT_USER = "your-mqtt-username"
MQTT_PASS = "your-mqtt-password"
```

### 2. Test it

```bash
~/pipboy-env/bin/python ~/pipboy_bridge.py
```

You should see:
```
INFO Opening /dev/ttyACM0 at 9600 baud
INFO Serial reader thread started
INFO MQTT connected
INFO Autodiscovery payloads published
INFO Clock synced: ...
INFO Firmware: 2v25.xxx
INFO Battery: xx%
```

Stop with Ctrl+C.

### 3. Install as a systemd service

```bash
sudo nano /etc/systemd/system/pipboy.service
```

Paste the following, replacing `your-username` with your actual username:

```ini
[Unit]
Description=Pip-Boy MQTT Bridge
After=network-online.target
Wants=network-online.target

[Service]
ExecStartPre=/bin/sleep 10
ExecStart=/home/your-username/pipboy-env/bin/python /home/your-username/pipboy_bridge.py
Restart=on-failure
RestartSec=10
User=your-username

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable pipboy
sudo systemctl start pipboy
sudo systemctl status pipboy
```

The service will now start automatically on every boot, waiting for network before connecting.

---

## Home Assistant Setup

### Prerequisites

- [Mosquitto broker](https://github.com/home-assistant/addons/tree/master/mosquitto) add-on installed and running
- MQTT integration configured in HA pointing at the broker
- An HA user account created for the bridge (Settings → People → Add Person)

### Autodiscovery

No configuration needed. Once the bridge connects, a **Pip-Boy 3000 MkV** device will appear automatically in HA under Settings → Devices & Services → MQTT with the following entities:

| Entity | Type | Notes |
|--------|------|-------|
| Power | Switch | ON = awake, OFF = sleeping |
| Brightness | Number | Slider 0–20, greyed out while sleeping |
| Demo Mode | Switch | Greyed out while sleeping |
| Torch | Switch | Greyed out while sleeping |
| Battery | Sensor | % charged, polled every 60s |
| Firmware | Sensor | Espruino firmware version |

---

## MQTT Topics

| Topic | Direction | Values |
|-------|-----------|--------|
| `pipboy/status` | Bridge → HA | `online` / `offline` |
| `pipboy/power` | Bridge → HA | `ON` = awake, `OFF` = sleeping |
| `pipboy/power/set` | HA → Bridge | `ON` = wake, `OFF` = sleep |
| `pipboy/brightness` | Bridge → HA | `0`–`20` |
| `pipboy/brightness/set` | HA → Bridge | `0`–`20` |
| `pipboy/demo` | Bridge → HA | `ON` / `OFF` |
| `pipboy/demo/set` | HA → Bridge | `ON` / `OFF` |
| `pipboy/torch` | Bridge → HA | `ON` / `OFF` |
| `pipboy/torch/set` | HA → Bridge | `ON` / `OFF` |
| `pipboy/battery` | Bridge → HA | `0`–`100` |
| `pipboy/firmware` | Bridge → HA | version string |

---

## Troubleshooting

**Pip-Boy not detected (`/dev/ttyACM*` missing)**
- Check you're using a data-capable USB-C cable — charge-only cables have no data lines
- Confirm the cable is in the Pi's right (OTG) port, not the left (power) port
- Check `config.txt` has `dtoverlay=dwc2` and `cmdline.txt` has `modules-load=dwc2,g_serial`

**MQTT connection fails (rc=5)**
- rc=5 is authentication failure — the Mosquitto addon uses HA user accounts, not its own login system. Create the user via Settings → People, not the Mosquitto addon UI.

**Commands reach the bridge but don't execute on the device**
- This is usually a serial buffer issue. The bridge sends Ctrl+C before each command to clear REPL state. If you see `:command` echoed back in the logs, the REPL is in a confused state — power cycle the Pip-Boy and restart the bridge.

**Service not starting on boot**
- Check `sudo systemctl status pipboy` for errors
- Ensure the path in the service file matches your actual username
- Try increasing the `ExecStartPre` sleep value if the network isn't ready in time

---

## Credits

- Serial command implementations from [CodyTolene/pip-terminal](https://github.com/CodyTolene/pip-terminal)
- Pip-Boy hardware documentation from [RobCo Industries](https://log.robco-industries.org) (Darrian)
- Community guides from [beaverboy-12/The-Wand-Company-Pip-Boy-3000-Mk-V-Community-Guide](https://github.com/beaverboy-12/The-Wand-Company-Pip-Boy-3000-Mk-V-Community-Guide)
- Pip-Boy apps and tooling by [Cody Tolene](https://www.codytolene.com)

---

## Disclaimer

Not affiliated with The Wand Company or Bethesda Softworks. Pip-Boy is a trademark of Bethesda Softworks LLC.
