# Brewing counts by time of day

The optional Daedalus **Reported brewing time buckets** sensor exposes up to eight
three-hour counts reported by the machine. It requires cloud telemetry and appears
once the machine provides a valid count. The sensor is disabled by default.

1. In **Settings → Devices & services → Devices**, open your machine. Find the sensor
   under its diagnostic entities, including disabled entities, and enable it.
2. Copy the sensor's entity ID. Add a **Manual** dashboard card and paste the
   [Brewing hours card](daedalus-brewing-hours-card.yaml). Replace the example
   entity ID with yours.

The card shows raw counts from `BrewingStatisticsTotals.TimeOfDay`. The vendor's
time basis and reset behavior may vary by machine and firmware. The integration
preserves the hour buckets as labeled by the machine. A dash means that a bucket
is unavailable. A reported zero displays as zero. The bars are scaled to the
largest available count. The entity's state counts
available buckets, and its attributes contain the individual counts.
