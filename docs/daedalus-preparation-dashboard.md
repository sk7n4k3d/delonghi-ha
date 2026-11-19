# Daily and weekly coffee preparations

This example uses Home Assistant's built-in [Utility Meter helpers](https://www.home-assistant.io/integrations/utility_meter/)
and dashboard cards. It requires a Daedalus version with lifetime brewing statistics
([PR #58](https://github.com/sk7n4k3d/delonghi-ha/pull/58)), cloud telemetry enabled and a machine
that reports the **Brew**, **Stopped** and **In error** counters. The dashboard keeps the three
reported outcomes separate.

## Set up the meters and card

1. Find your machine in **Settings → Devices & services → Devices**. Open each of the three
   lifetime sensors and copy its entity ID. Wait for numeric readings before setting up the meters.
2. Copy [the Utility Meter example](daedalus-preparation-meters.yaml) into `configuration.yaml`.
   If you already have a `utility_meter:` section, add the entries under it. Replace all
   `sensor.example_…` IDs with the corresponding IDs from your machine.
3. Check the configuration in **Developer tools → YAML**, then restart Home Assistant.
   Verify the resulting meter IDs in **Developer tools → States**. Change the YAML keys before
   setup if they conflict with existing entities or you are setting up another machine.
4. Edit your dashboard, add a **Manual** card and paste [the card YAML](daedalus-preparation-card.yaml).
   Update the source IDs and any meter IDs that differ from the example. If your machine exposes
   only some of the counters, remove the missing sources and their meter entries and card rows.

## Reading the counts

- The first numeric reading establishes the starting point. The first day and week are partial.
  Daily meters roll over at midnight and weekly meters on Monday at midnight in Home Assistant's
  configured time zone.
- An `unknown` or `unavailable` source reading supplies no new count. Check the lifetime source
  rows when interpreting the totals. With **Periodically resetting** off, the meters can use
  the difference from the last valid reading after telemetry returns. The increment appears in
  the period when Home Assistant receives it. A delay or outage spanning a rollover can therefore
  assign events to the next day or week.
- If the lifetime counter decreases after a reset, Utility Meter ignores the negative change
  and uses the lower reading as its new baseline.
