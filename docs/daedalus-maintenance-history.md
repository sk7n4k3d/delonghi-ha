# Manual coffee machine maintenance history

Use this optional package with Home Assistant 2024.11 or later to save the last time you logged a
descale, water filter replacement or brew unit cleaning. Log each task after completing it on the
machine. The Home Assistant action supplies the timestamps and works with LAN-only setups or with
the appliance offline.

## Set up the package

1. Copy [daedalus-maintenance-history.yaml](daedalus-maintenance-history.yaml) into the `packages`
   directory in your Home Assistant configuration directory. To enable
   [packages](https://www.home-assistant.io/docs/configuration/packages/) for the first time, create
   the directory and add the following to `configuration.yaml`. If the file already contains
   `homeassistant:`, add `packages:` under that key.

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. In **Developer tools → YAML**, check the configuration, then restart Home Assistant.
3. In **Developer tools → Actions**, select **Coffee maintenance: Log completed task**, choose the
   completed task and perform the action. Each timestamp sensor starts as **Unknown** and
   shows a date and time after you first log its task. Home Assistant restores the latest timestamps
   on restart. With Recorder enabled for these sensors, their History also shows previous entries
   within your retention period.
4. For dashboard access, add a **Manual** card and paste
   [daedalus-maintenance-card.yaml](daedalus-maintenance-card.yaml). Check the script and sensor
   entity IDs in **Developer tools → States** and adjust the card if your installation assigned
   different IDs.

## Set up another machine

Save another copy of the package with a different filename. Before restarting, replace
every `coffee_maintenance` in that copy with a distinct lowercase prefix such as
`office_coffee_maintenance`, and change the display label `Coffee maintenance` to
`Office coffee maintenance`. Check the new entity IDs when copying the dashboard card, and
give the card a title that identifies the machine. Each machine then has its own logging
action and timestamps. Keep the prefixes fixed after logging items to preserve the sensors'
registered identities.
