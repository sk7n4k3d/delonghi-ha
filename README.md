# De'Longhi Coffee for Home Assistant

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

One button per available beverage on your machine (Espresso, Cappuccino, Latte Macchiato, etc.). Beverages are auto-discovered from your machine's stored recipes.

## Screenshots

*Coming soon*

## Technical Details

This integration communicates with the De'Longhi Coffee Link cloud service:

1. **Authentication**: Gigya (identity provider) -> JWT -> Ayla Networks token
2. **Device control**: Ayla Networks IoT cloud API (device properties, ECAM commands)
3. **Protocol**: ECAM binary commands wrapped in Base64, sent via Ayla datapoints

The API credentials in the code are **public app-level keys** — they are the same keys embedded in the official De'Longhi Coffee Link Android app and are shared by all users. They are not user secrets.

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

- Reverse-engineered from the De'Longhi Coffee Link Android app
- Built on the Ayla Networks IoT platform API
- ECAM protocol analysis via MITM capture

## License

MIT
