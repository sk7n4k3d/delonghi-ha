# Coffee machine care dashboard

The PrimaDonna Aromatic ECAM630.70 reports these settings through optional, read-only
[Daedalus cloud telemetry](daedalus-setup.md). Other models may report some of the same fields.
Each entity appears after the integration receives its first valid value. The integration normally
checks these settings approximately every 15 minutes when cloud status is available.

| Entity | Reading |
| --- | --- |
| Descale progress | Percentage of the machine's descaling interval used |
| Water filter usage | Percentage of the water filter's service interval used |
| Grounds container fill | Machine-reported grounds container fill percentage |
| Auto-off delay | Configured automatic shutoff delay in minutes |
| Water filter installed | Whether a filter is registered as installed in the machine settings |

The configured auto-off delay is a fixed setting. The filter installation indicator and the
**Replace water filter** alarm are separate. Use the machine's menu to change the delay or register
a filter. Water filter usage is unavailable when the machine explicitly reports that no filter is
installed. Models that omit the installation setting may still report filter usage.

## Add the dashboard card

1. Enable cloud telemetry and open your coffee machine under **Settings → Devices & services → Devices**.
   Copy the entity IDs of the available readings.
2. Edit your dashboard, add a **Manual** card, and paste the [example card](daedalus-care-card.yaml).
3. Replace the example entity IDs with the IDs for one machine and remove rows for readings that your
   machine does not report. Copy the card and select a different set of entities for another coffee machine.

## Get a maintenance early warning

1. On Home Assistant 2024.11 or later, copy the [early warning blueprint](../blueprints/automation/daedalus-maintenance-early-warning.yaml)
   to `blueprints/automation/daedalus/daedalus-maintenance-early-warning.yaml` in your Home Assistant
   configuration directory.
2. In **Settings → Automations & scenes → Blueprints**, open **De'Longhi My Coffee Lounge maintenance early warning**
   and create an automation. Select **Descale progress** or **Water filter usage** for one machine,
   set the warning threshold, and select your Companion app phone. The default threshold is 80%.
3. Optionally choose a Shopping list or another `todo` entity and enter an item such as
   **De'Longhi water filter** or **De'Longhi descaler**. Leave the list and item empty for phone-only warnings.
4. Create a separate automation for each maintenance reading or machine you want to track.

A warning is sent when two consecutive available readings cross the threshold from below.
For example, with an 80% threshold, a change from 78% to 82% sends one warning. Later readings
above the threshold stay quiet. The first reading and the first reading after a restart or telemetry
outage establish a baseline. A reading below the threshold, such as after replacing and registering
a filter, allows another warning on the next crossing.

An open list item with the same name, ignoring case and leading or trailing spaces, is reused.
Mark the item complete when you buy it. After the percentage falls below the threshold and then
crosses it again, Home Assistant can add a fresh item.
If two automations reach their thresholds at the same time, each can add the same item to a
shared list before the other sees it.

See the [maintenance alarm blueprint](../blueprints/automation/daedalus-maintenance-notifications.yaml)
for notifications when the machine raises an alarm, and the [observed maintenance history](daedalus-observed-maintenance.md)
for timestamps based on lifetime counters.
