#!/usr/bin/env python3
# pipboy_bridge.py
# MQTT <-> Pip-Boy serial bridge for Raspberry Pi Zero 2W
#
# Commands sourced directly from CodyTolene/pip-terminal command files.
#
# MQTT command topics:
#   pipboy/power/set          ON = wake device, OFF = sleep device
#   pipboy/brightness/set     0-20 integer
#   pipboy/demo/set           ON / OFF  (unavailable while sleeping)
#   pipboy/torch/set          ON / OFF  (unavailable while sleeping)
#
# MQTT state/sensor topics:
#   pipboy/power              ON = awake, OFF = sleeping
#   pipboy/brightness         0-20
#   pipboy/demo               ON / OFF
#   pipboy/torch              ON / OFF
#   pipboy/battery            0-100 (%)
#   pipboy/firmware           firmware version string
#   pipboy/status             online / offline

import serial
import paho.mqtt.client as mqtt
import time
import logging
import json
import threading
import queue
import signal
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

SERIAL_PORT      = "/dev/ttyACM0"
BAUD             = 9600
MQTT_HOST        = "YOUR_HA_IP"
MQTT_PORT        = 1883
MQTT_USER        = "YOUR_MQTT_USER"
MQTT_PASS        = "YOUR_MQTT_PASS"
CLIENT_ID        = "pipboy_bridge"
DISCOVERY_PREFIX = "homeassistant"
POLL_INTERVAL    = 60   # seconds between battery polls

# Topics
T_STATUS      = "pipboy/status"
T_COMMAND     = "pipboy/command"
T_POWER_SET   = "pipboy/power/set"
T_POWER_PUB   = "pipboy/power"       # ON = awake, OFF = sleeping
T_BRIGHT_SET  = "pipboy/brightness/set"
T_BRIGHT_PUB  = "pipboy/brightness"
T_DEMO_SET    = "pipboy/demo/set"
T_DEMO_PUB    = "pipboy/demo"
T_TORCH_SET   = "pipboy/torch/set"
T_TORCH_PUB   = "pipboy/torch"
T_BATTERY     = "pipboy/battery"
T_FIRMWARE    = "pipboy/firmware"

BRIGHT_MIN = 0
BRIGHT_MAX = 20

serial_lock = threading.Lock()
response_q  = queue.Queue()
stop_event  = threading.Event()


# ── Serial helpers ─────────────────────────────────────────────────────────────

def flush(ser):
    time.sleep(0.05)
    waiting = ser.in_waiting
    if waiting:
        ser.read(waiting)


def send(ser, cmd):
    """Send a JS command — Ctrl+C to clear, echo(0) to suppress output,
    then the command. Buffer drained after each step."""
    with serial_lock:
        logging.info(f"Serial TX: {cmd!r}")
        ser.write(b"\x03")
        flush(ser)
        ser.write(b"echo(0)\n")
        flush(ser)
        ser.write((cmd + "\n").encode())
        flush(ser)


def query(ser, cmd, timeout=3.0):
    """Send a JS expression and return the result string, or None."""
    while not response_q.empty():
        try:
            response_q.get_nowait()
        except queue.Empty:
            break

    with serial_lock:
        ser.write(b"\x03")
        flush(ser)
        ser.write(b"echo(1)\n")
        flush(ser)
        ser.write((cmd + "\n").encode())

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            line = response_q.get(timeout=0.2).strip()
            if line.startswith("="):
                with serial_lock:
                    ser.write(b"echo(0)\n")
                    flush(ser)
                return line[1:].strip('"')
        except queue.Empty:
            continue

    with serial_lock:
        ser.write(b"echo(0)\n")
        flush(ser)
    return None


# ── Command implementations (from pip-terminal source) ─────────────────────────

def sleep_device(ser):
    send(ser, (
        "(() => {"
        " Pip.sleeping = true;"
        " Pip.offOrSleep({ immediate: false, forceOff: false, playSound: true });"
        " })()"
    ))


def wake_device(ser):
    send(ser, (
        "(() => {"
        " if (Pip.sleeping) {"
        "  Pip.sleeping = false;"
        "  Pip.wake();"
        "  Pip.brightness = 20;"
        "  Pip.addWatches();"
        "  setTimeout(() => { Pip.fadeOn([LCD_BL, LED_RED, LED_GREEN]); }, 100);"
        "  showMainMenu();"
        " }"
        " return Pip.sleeping;"
        "})()"
    ))


def get_battery(ser):
    """Measure battery via Pip.measurePin(VBAT_MEAS), return % or None."""
    result = query(ser, (
        "(() => {"
        " try {"
        "  let v = Pip.measurePin(VBAT_MEAS);"
        "  let pct = Math.min(100, Math.max(0, ((v - 3.0) / (4.2 - 3.0)) * 100));"
        "  return pct.toFixed(0);"
        " } catch(e) { return null; }"
        " })()"
    ))
    if result and result != "null":
        try:
            return int(float(result))
        except ValueError:
            pass
    return None


def get_firmware(ser):
    return query(ser, "process.env.VERSION")


def sync_clock(ser):
    """Sync Pip-Boy clock to Pi system time."""
    ts = int(time.time())
    tz = -time.timezone / 3600
    send(ser, (
        f"(() => {{"
        f" try {{"
        f"  setTime({ts});"
        f"  E.setTimeZone({tz});"
        f"  settings.timezone = {tz};"
        f"  settings.century = 20;"
        f"  saveSettings();"
        f"  tm0 = null;"
        f"  if (typeof drawFooter === 'function') drawFooter();"
        f" }} catch(e) {{}}"
        f" }})()"
    ))
    logging.info(f"Clock synced: ts={ts} tz={tz}")


# ── Serial reader thread ───────────────────────────────────────────────────────

def serial_reader(ser):
    logging.info("Serial reader thread started")
    while not stop_event.is_set():
        try:
            data = ser.read(ser.in_waiting or 1)
            if data:
                text = data.decode(errors="replace")
                for line in text.splitlines():
                    if line.strip():
                        logging.info(f"Serial RAW RX: {line!r}")
                        response_q.put(line)
        except serial.SerialException as e:
            logging.error(f"Serial read error: {e}")
            time.sleep(1)
        except Exception as e:
            logging.error(f"Reader thread error: {e}")
            time.sleep(1)


# ── Battery poller thread ──────────────────────────────────────────────────────

def battery_poller(ser, mqtt_client):
    logging.info("Battery poller thread started")
    while not stop_event.is_set():
        stop_event.wait(POLL_INTERVAL)
        if stop_event.is_set():
            break
        level = get_battery(ser)
        if level is not None:
            mqtt_client.publish(T_BATTERY, str(level), retain=True)
            logging.info(f"Battery: {level}%")
        else:
            logging.warning("Battery read returned None")


# ── Autodiscovery ──────────────────────────────────────────────────────────────

def publish_discovery(client):
    device = {
        "identifiers": ["pipboy_3000_mkv"],
        "name": "Pip-Boy 3000 MkV",
        "model": "3000 Mk V",
        "manufacturer": "The Wand Company",
    }

    # Standard availability — bridge must be online
    avail_bridge = {
        "topic": T_STATUS,
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    # Availability for entities that require device to be awake
    # Both conditions must be true: bridge online AND device not sleeping
    avail_awake = [
        avail_bridge,
        {
            "topic": T_POWER_PUB,
            "payload_available": "ON",
            "payload_not_available": "OFF",
        },
    ]

    entities = [
        # Power switch — ON = awake, OFF = sleeping
        (f"{DISCOVERY_PREFIX}/switch/pipboy/power/config", {
            "name": "Power",
            "unique_id": "pipboy_power",
            "icon": "mdi:power",
            "state_topic": T_POWER_PUB,
            "command_topic": T_POWER_SET,
            "payload_on": "ON",
            "payload_off": "OFF",
            "retain": True,
            "availability": avail_bridge,
            "device": device,
        }),
        # Brightness slider — only usable when awake
        (f"{DISCOVERY_PREFIX}/number/pipboy/brightness/config", {
            "name": "Brightness",
            "unique_id": "pipboy_brightness",
            "icon": "mdi:brightness-6",
            "state_topic": T_BRIGHT_PUB,
            "command_topic": T_BRIGHT_SET,
            "min": BRIGHT_MIN,
            "max": BRIGHT_MAX,
            "step": 1,
            "retain": True,
            "availability": avail_awake,
            "availability_mode": "all",
            "device": device,
        }),
        # Demo mode — unavailable while sleeping
        (f"{DISCOVERY_PREFIX}/switch/pipboy/demo/config", {
            "name": "Demo Mode",
            "unique_id": "pipboy_demo",
            "icon": "mdi:presentation-play",
            "state_topic": T_DEMO_PUB,
            "command_topic": T_DEMO_SET,
            "payload_on": "ON",
            "payload_off": "OFF",
            "retain": True,
            "availability": avail_awake,
            "availability_mode": "all",
            "device": device,
        }),
        # Torch — unavailable while sleeping
        (f"{DISCOVERY_PREFIX}/switch/pipboy/torch/config", {
            "name": "Torch",
            "unique_id": "pipboy_torch",
            "icon": "mdi:flashlight",
            "state_topic": T_TORCH_PUB,
            "command_topic": T_TORCH_SET,
            "payload_on": "ON",
            "payload_off": "OFF",
            "retain": True,
            "availability": avail_awake,
            "availability_mode": "all",
            "device": device,
        }),
        # Battery sensor
        (f"{DISCOVERY_PREFIX}/sensor/pipboy/battery/config", {
            "name": "Battery",
            "unique_id": "pipboy_battery",
            "icon": "mdi:battery",
            "state_topic": T_BATTERY,
            "unit_of_measurement": "%",
            "device_class": "battery",
            "state_class": "measurement",
            "availability": avail_bridge,
            "device": device,
        }),
        # Firmware sensor
        (f"{DISCOVERY_PREFIX}/sensor/pipboy/firmware/config", {
            "name": "Firmware",
            "unique_id": "pipboy_firmware",
            "icon": "mdi:chip",
            "state_topic": T_FIRMWARE,
            "availability": avail_bridge,
            "device": device,
        }),
    ]

    for topic, payload in entities:
        client.publish(topic, json.dumps(payload), retain=True)

    logging.info("Autodiscovery payloads published")


# ── MQTT callbacks ─────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    ser = userdata
    if rc == 0:
        logging.info("MQTT connected")
        client.publish(T_STATUS, "online", retain=True)
        publish_discovery(client)
        for t in [T_COMMAND, T_POWER_SET, T_BRIGHT_SET, T_DEMO_SET, T_TORCH_SET]:
            client.subscribe(t)

        # Assume awake on connect
        client.publish(T_POWER_PUB, "ON", retain=True)

        # Sync clock on connect
        sync_clock(ser)

        # Read firmware version
        fw = get_firmware(ser)
        if fw:
            client.publish(T_FIRMWARE, fw, retain=True)
            logging.info(f"Firmware: {fw}")

        # Initial battery read
        time.sleep(0.5)
        level = get_battery(ser)
        if level is not None:
            client.publish(T_BATTERY, str(level), retain=True)
            logging.info(f"Battery: {level}%")
    else:
        logging.error(f"MQTT connect failed rc={rc}")


def on_disconnect(client, userdata, rc):
    logging.warning(f"MQTT disconnected rc={rc}")


def on_message(client, userdata, msg):
    ser     = userdata
    topic   = msg.topic
    payload = msg.payload.decode().strip()
    logging.info(f"MQTT <- {topic}: {payload}")

    if topic in (T_COMMAND, T_POWER_SET):
        if payload == "OFF":
            # Toggle OFF = sleep the device
            sleep_device(ser)
            client.publish(T_POWER_PUB, "OFF", retain=True)
            # Mark demo and torch as off when sleeping
            client.publish(T_DEMO_PUB, "OFF", retain=True)
            client.publish(T_TORCH_PUB, "OFF", retain=True)
        elif payload == "ON":
            # Toggle ON = wake the device
            wake_device(ser)
            client.publish(T_POWER_PUB, "ON", retain=True)

    elif topic == T_BRIGHT_SET:
        try:
            level = max(BRIGHT_MIN, min(BRIGHT_MAX, int(payload)))
            send(ser, f"Pip.brightness={level};Pip.updateBrightness()")
            client.publish(T_BRIGHT_PUB, str(level), retain=True)
        except ValueError:
            logging.warning(f"Invalid brightness: {payload}")

    elif topic == T_DEMO_SET:
        if payload == "ON":
            send(ser, "enterDemoMode()")
            client.publish(T_DEMO_PUB, "ON", retain=True)
        elif payload == "OFF":
            send(ser, "leaveDemoMode()")
            client.publish(T_DEMO_PUB, "OFF", retain=True)

    elif topic == T_TORCH_SET:
        if payload == "ON":
            send(ser, "showTorch()")
            client.publish(T_TORCH_PUB, "ON", retain=True)
        elif payload == "OFF":
            send(ser, "showMainMenu()")
            client.publish(T_TORCH_PUB, "OFF", retain=True)


# ── Signal handler ─────────────────────────────────────────────────────────────

def handle_signal(sig, frame):
    logging.info("Shutting down...")
    stop_event.set()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logging.info(f"Opening {SERIAL_PORT} at {BAUD} baud")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        logging.error(f"Failed to open serial: {e}")
        sys.exit(1)

    time.sleep(2)
    flush(ser)

    threading.Thread(target=serial_reader, args=(ser,), daemon=True).start()

    client = mqtt.Client(client_id=CLIENT_ID, userdata=ser)
    client.will_set(T_STATUS, "offline", retain=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    threading.Thread(
        target=battery_poller, args=(ser, client), daemon=True
    ).start()

    client.loop_start()
    stop_event.wait()

    logging.info("Cleaning up")
    client.publish(T_STATUS, "offline", retain=True)
    time.sleep(0.2)
    client.loop_stop()
    client.disconnect()
    ser.close()
    logging.info("Done")


if __name__ == "__main__":
    main()
