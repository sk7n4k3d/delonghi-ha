# De'Longhi Coffee for Home Assistant

> **AI Disclosure:** This integration was developed with the assistance of Claude (Anthropic). The protocol reverse engineering, code architecture, and implementation were produced in collaboration with AI. All code has been reviewed, tested on real hardware, and audited for security.

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/sk7n4k3d/delonghi-ha)](https://github.com/sk7n4k3d/delonghi-ha/releases)

Home Assistant custom integration for De'Longhi connected coffee machines (Eletta Explore, Dinamica Plus, Rivelia, etc.) via the De'Longhi Coffee Link cloud API.

## Features

- **Machine status** — real-time state (Off, Idle, Heating, Ready, Brewing, Error, Descaling)
- **19 alarm sensors** — water tank, grounds container, descale needed, beans empty, drip tray, and more
- **17 counter sensors** — total beverages, per-drink counters (espresso, cappuccino, latte...), grounds ejected, total water used
- **Brew buttons** — one button per available beverage, using your personalized recipes stored on the machine
- **Cloud polling** with configurable interval (default: 30 seconds)
- **Automatic token refresh** and retry logic with backoff
- **Reauth flow** — seamless re-authentication when credentials expire

## Supported Machines

Any De'Longhi WiFi-connected coffee machine that works with the **De'Longhi Coffee Link** app, including:

- Eletta Explore (ECAM450.xx)
- Dinamica Plus (ECAM370.xx)
- Rivelia (EXAM440.xx)
- Perfetto (ECAM550.xx)
- Other Ayla Networks-connected models

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu -> **Custom repositories**
3. Add `https://github.com/sk7n4k3d/delonghi-ha` as an **Integration**
4. Search for "De'Longhi Coffee" and install
5. Restart Home Assistant

### Manual

1. Copy `custom_components/delonghi_coffee/` to your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings** -> **Devices & Services** -> **Add Integration**
2. Search for **De'Longhi Coffee**
3. Enter your **De'Longhi Coffee Link** account credentials (same as the mobile app)
4. The integration will discover your machine(s) automatically

## Entities

### Sensors

| Entity | Description |
|--------|-------------|
| `sensor.coffee_machine_status` | Machine state (Off, Idle, Ready, Brewing...) |
| `sensor.coffee_total_beverages` | Total beverages brewed |
| `sensor.coffee_espressos` | Espresso counter |
| `sensor.coffee_grounds_container` | Grounds container fill percentage |
| ... | Per-drink counters, total water, descale count |

### Binary Sensors (Alarms)

| Entity | Description |
|--------|-------------|
| `binary_sensor.coffee_water_tank_empty` | Water tank needs refilling |
| `binary_sensor.coffee_grounds_container_full` | Grounds container needs emptying |
| `binary_sensor.coffee_descale_needed` | Descaling required |
| `binary_sensor.coffee_coffee_beans_empty` | Bean hopper is empty |
| ... | 19 alarm types total |

### Buttons

| Entity | Description |
|--------|-------------|
| `button.power_on` | Wake machine from standby |
| `button.power_off` | Put machine in standby |
| `button.brew_espresso` | Brew an espresso |
| `button.brew_cappuccino` | Brew a cappuccino |
| `button.brew_latte_macchiato` | Brew a latte macchiato |
| ... | 50+ buttons, one per available beverage (auto-discovered) |

> **Known limitation:** Currently only the **Espresso** brew button is fully functional. Other beverages are detected and displayed but their brew commands are still being reverse-engineered. The stored recipe format (0xA6) differs from the brew command format (0x83) and we're working on the correct conversion. Power On/Off work for all models. See [#4](https://github.com/sk7n4k3d/delonghi-ha/issues/4) for progress.

## Screenshots

*Coming soon*

## Protocol Documentation

### Architecture

```
┌──────────┐     ┌──────────────┐     ┌──────────────┐     ┌─────────────┐
│  Home    │────▶│   Gigya SSO  │────▶│ Ayla Networks│────▶│  Coffee     │
│Assistant │     │ (De'Longhi)  │     │  IoT Cloud   │     │  Machine    │
└──────────┘     └──────────────┘     └──────────────┘     └─────────────┘
     │            accounts.eu1.        ads-eu.ayla           WiFi (Ayla
     │            gigya.com            networks.com          agent ESP32)
     │                                      │
     │  1. Login (email/password)           │
     │  2. Get JWT token                    │
     │  3. Ayla token_sign_in               │
     │  4. Read/Write device properties     │
     └─────────────────────────────────────┘
```

### Authentication Flow

1. **Gigya Login** — `POST accounts.eu1.gigya.com/accounts.login` with email, password, API key
2. **Get JWT** — `POST accounts.eu1.gigya.com/accounts.getJWT` with session token
3. **Ayla Token** — `POST user-field-eu.aylanetworks.com/api/v1/token_sign_in` with app_id, app_secret, JWT
4. Returns `access_token` (valid 24h) + `refresh_token`

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/apiv1/devices.json` | List all devices (DSN, model, IP, status) |
| `GET` | `/apiv1/dsns/{DSN}/properties.json` | Get all device properties |
| `GET` | `/apiv1/dsns/{DSN}/properties/{name}.json` | Get single property |
| `POST` | `/apiv1/dsns/{DSN}/properties/{name}/datapoints.json` | Write a property value |

Base URL: `https://ads-eu.aylanetworks.com`
Auth header: `Authorization: auth_token {token}`

### Key Properties

| Property | Direction | Description |
|----------|-----------|-------------|
| `app_data_request` | input | Send ECAM commands (Base64 encoded) |
| `app_data_response` | output | Machine response to commands |
| `app_device_connected` | input | Ping to force data refresh |
| `app_device_status` | output | Cloud status (RUN, etc.) |
| `d302_monitor_machine` | output | Real-time monitor (binary, MonitorDataV2) |
| `d510_ground_cnt_percentage` | output | Grounds container fill % |
| `d5xx_*` | output | Machine settings and maintenance |
| `d7xx_*` | output | Beverage counters |
| `d1xx_rec_*` | output | Recipe data (Base64 ECAM) |

### ECAM Packet Format

```
┌─────────┬────────┬─────────────┬───────────┬────────────┬───────────┐
│Direction│ Length │ Packet Data │ Checksum  │ Timestamp  │  App ID   │
│  1 byte │ 1 byte│  N bytes    │  2 bytes  │  4 bytes   │  4 bytes  │
└─────────┴────────┴─────────────┴───────────┴────────────┴───────────┘
│◄──────── Base64 encoded ──────────────────────────────────────────►│
```

- **Direction**: `0x0D` (13) for queries, `0xD0` (208) for answers
- **Length**: N + 3 (packet data + length + checksum bytes)
- **Checksum**: CRC-16/SPI-FUJITSU over Direction + Length + Packet Data
- **Timestamp**: Unix time in seconds (4 bytes big-endian)
- **App ID**: `0x204035EF` (constant app signature)

### CRC-16 Algorithm

```python
def crc16(data: bytes) -> bytes:
    crc = 0x1D0F
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc = crc << 1
    crc &= 0xFFFF
    return crc.to_bytes(2, byteorder='big')
```

### ECAM Commands

| Request ID | Hex | Name | Contents | Description |
|------------|-----|------|----------|-------------|
| 132 | 0x84 | Application Control | `0x02, 0x01` | **Power On** (wake from standby) |
| 132 | 0x84 | Application Control | `0x01, 0x01` | **Power Off** (enter standby) |
| 132 | 0x84 | Application Control | `0x03, 0x02` | Connection refresh |
| 131 | 0x83 | Brew Beverage | *recipe data* | Prepare a beverage |
| 117 | 0x75 | MonitorV2 | *(none)* | Request machine status |

### Monitor Data (MonitorDataV2)

The `d302_monitor_machine` property contains a Base64-encoded binary with this layout:

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0 | 1 | Accessory | Connected accessory (0=none, 1=hot water, 2=latte crema hot...) |
| 1-2 | 2 | Switches | Bit field — water tank, motor, spout, door, etc. |
| 3-4 | 2 | Alarms[0-1] | Bit field — water empty, grounds full, descale, beans... |
| 5 | 1 | Status | Machine state (0=standby, 2=going to sleep, 7=ready, 3=brewing...) |
| 6 | 1 | Step | Current action step |
| 7 | 1 | Progress | Brew progress 0-100% |
| 8-9 | 2 | Alarms[2-3] | More alarm bits (cleaning needed, hopper absent...) |
| 10-11 | 2 | Reserved | Always 0x00 |
| 12 | 1 | Reserved | Always 0x00 |

### Machine States

| Value | State |
|-------|-------|
| 0 | Standby |
| 1 | Waking up |
| 2 | Going to sleep |
| 4 | Descaling |
| 5 | Preparing steam |
| 7 | Ready |
| 8 | Rinsing |
| 10 | Preparing milk |
| 11 | Dispensing hot water |

### Alarm Bits (32-bit word)

| Bit | Alarm |
|-----|-------|
| 0 | Water tank empty |
| 1 | Grounds container full |
| 2 | Descale needed |
| 3 | Replace water filter |
| 4 | Coffee ground too fine |
| 5 | Coffee beans empty |
| 6 | Machine service required |
| 7 | Heater probe failure |
| 13 | Water tank not in position |
| 16 | Cleaning needed |

### Forcing Data Refresh

The machine only pushes counter/stat updates when pinged:

```
POST /apiv1/dsns/{DSN}/properties/app_device_connected/datapoints.json
Body: {"datapoint": {"value": "<base64 of timestamp + app_id>"}}
```

Wait 5-10 seconds, then read properties — counters will be fresh.

### API Credentials

The API credentials in the code are **public app-level keys** — they are the same keys embedded in the official De'Longhi Coffee Link Android app and are shared by all users. They are not user secrets.

- **Gigya API Key**: identifies the De'Longhi app to the Gigya identity platform
- **Ayla app_id / app_secret**: identifies the Coffee Link app to the Ayla IoT cloud

## Automations Examples

```yaml
# Notify when grounds container is full
automation:
  - alias: "Coffee grounds full notification"
    trigger:
      - platform: state
        entity_id: binary_sensor.coffee_grounds_container_full
        to: "on"
    action:
      - service: notify.mobile_app
        data:
          title: "Coffee Machine"
          message: "Grounds container is full, please empty it!"

# Morning espresso at 7:00
automation:
  - alias: "Morning espresso"
    trigger:
      - platform: time
        at: "07:00:00"
    condition:
      - condition: state
        entity_id: sensor.coffee_machine_status
        state: "Ready"
    action:
      - service: button.press
        target:
          entity_id: button.brew_espresso
```

## Credits

- Reverse-engineered from the De'Longhi Coffee Link Android app via MITM + jadx decompilation
- Protocol documentation by [MattG-K](https://framagit.org/mattgk/dlghiot) (CRC-16 algorithm, standby command, monitor data format)
- Built on the [Ayla Networks](https://www.aylanetworks.com/) IoT platform API
- [ayla-iot-unofficial](https://github.com/rewardone/ayla-iot-unofficial) Python library for reference
- [AylaLocalAPI](https://github.com/jakecrowley/AylaLocalAPI) for LAN protocol reference

## License

MIT
