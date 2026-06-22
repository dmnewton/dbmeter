# dbmeter

A sound level meter that measures dBA and publishes readings to Home Assistant via MQTT.

## Dependencies

Install the required system packages:

```bash
sudo apt-get install python3-pyaudio python3-yaml python3-numpy python3-scipy python3-paho-mqtt python3-dotenv
```

| Package | Purpose |
|---|---|
| `python3-pyaudio` | Audio capture from the microphone |
| `python3-yaml` | Parsing `config.yaml` |
| `python3-numpy` | Signal processing and RMS calculation |
| `python3-scipy` | A-weighting IIR filter (`bilinear`, `lfilter`) |
| `python3-paho-mqtt` | MQTT communication with Home Assistant |
| `python3-dotenv` | Loading `MQTT_PASSWORD` from `.env` |

## Configuration

Copy the example environment file and set your MQTT password:

```bash
echo "MQTT_PASSWORD=your_password_here" > .env
```

Edit `config.yaml` to match your setup (MQTT host, device ID, audio format, etc.).

## Running as a systemd Service

The included `dbmeter.service` file configures the process to start automatically on boot.

### Install the service

```bash
sudo cp dbmeter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable dbmeter.service
sudo systemctl start dbmeter.service
```

### Managing the service

```bash
# Check status
sudo systemctl status dbmeter.service

# View logs
journalctl -u dbmeter.service -f

# Stop the service
sudo systemctl stop dbmeter.service

# Disable autostart
sudo systemctl disable dbmeter.service

```

### Hardware Connections (Wiring)

| Microphone Pin | Pin Function | Raspberry Pi Zero Pin | GPIO/BCM Number |
| :--- | :--- | :--- | :--- |
| **VCC / 3V** | 3.3V Power | Pin 1 or Pin 17 | — |
| **GND** | Ground | Pin 6 (or any GND pin) | — |
| **BCLK** | Bit Clock | Pin 12 | GPIO 18 |
| **DOUT / SD** | Data Out / Serial Data | Pin 38 | GPIO 20 |
| **LRCL / WS** | Left/Right Clock / Word Select | Pin 35 | GPIO 19 |
| **SEL / L/R** | Channel Select | Connect to **GND** (for Left) or **3.3V** (for Right) | — |



### Notes

- The service runs as user `dnewton` from the working directory `/home/dnewton/dbmeter`. Adjust the `User` and `WorkingDirectory` fields in `dbmeter.service` if your setup differs.
- The service restarts automatically after 30 seconds if it crashes.
- The measurement interval can be controlled dynamically from Home Assistant and is persisted across restarts in `measurement_interval.txt`.
