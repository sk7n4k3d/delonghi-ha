# Observed coffee machine maintenance history

Home Assistant can save a timestamp when it sees **Descale count** or **Water filter registration
count** increase. The filter count can include first installation as well as later registrations.
Use the [manual maintenance history](daedalus-maintenance-history.md) to log a physical filter
replacement, brew unit cleaning or a task completed while telemetry was unavailable. You can use
the manual history with LAN-only setups.

## Set up observed timestamps

1. Enable optional [Daedalus cloud telemetry](daedalus-setup.md) and find your machine under
   **Settings → Devices & services → Devices**. Copy the entity ID of each maintenance count
   sensor your machine reports.
2. On Home Assistant 2024.11 or later, copy the [template blueprint](../blueprints/template/daedalus-observed-maintenance.yaml)
   to `blueprints/template/daedalus/daedalus-observed-maintenance.yaml` in your Home Assistant
   configuration directory.
3. Copy [the example package](daedalus_observed_maintenance.yaml) to the `packages` directory.
   If you need to enable packages, follow [the package setup instructions](daedalus-maintenance-history.md#set-up-the-package).
   Replace each `sensor.example_…` with the corresponding entity ID for your machine. Remove the
   entire `use_blueprint` entry for any count your machine does not report.
4. For a second machine, copy the package to a different filename and configure its own source
   entities. Give each timestamp sensor a distinct name and `unique_id`. Keep each `unique_id`
   fixed after setup to preserve the sensor's registered identity.
5. In **Developer tools → YAML**, check the configuration, then restart Home Assistant. Verify the
   timestamp sensor IDs in **Developer tools → States**. Add the sensors to an **Entities** card
   to see the latest dates on your dashboard.

## Read the history

- Each timestamp starts as **Unknown**. Home Assistant records one observation when the selected
  counter changes from one valid reading to a higher valid reading, even if it increases by
  several counts in one poll. The timestamp reflects when Home Assistant processed the increase.
- The first reading, a return from `unknown` or `unavailable`, and a decrease establish a new
  baseline for future comparisons. A repeated value or a change to an alarm leaves the timestamp
  unchanged. Observations start at setup and may lag maintenance by a telemetry update interval
  or longer.
- Home Assistant restores the latest timestamp on restart. With Recorder enabled for the timestamp
  sensor, **History** also shows previous observations within your retention period.
