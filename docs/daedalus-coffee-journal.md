# Coffee and grinder journal

Record the beans and grinder setting you used, with an optional tasting or brewing note.
This Home Assistant package works locally while the machine is offline and keeps a separate
journal for each machine. Saving an entry does not read or change any setting on the appliance.

## Set up the journal

1. Copy [daedalus_coffee_journal.yaml](daedalus_coffee_journal.yaml) into the `packages` directory
   in your Home Assistant configuration directory. If you have yet to enable packages, create the
   directory and add the following to `configuration.yaml`. If `homeassistant:` already exists,
   add `packages:` under that key instead. [Home Assistant packages](https://www.home-assistant.io/docs/configuration/packages/)

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Home Assistant's default configuration includes **Activity** (called **Logbook** on
   older releases). If you use a minimal configuration, add `logbook:` to
   `configuration.yaml` to enable recent entries. In **Developer tools → YAML**, check the
   configuration and restart Home Assistant. [Home Assistant Activity](https://www.home-assistant.io/integrations/logbook/)
3. Add a **Manual** dashboard card and paste [daedalus-coffee-journal-card.yaml](daedalus-coffee-journal-card.yaml).
   Verify the script, inputs and sensors in **Developer tools → States**. If Home Assistant
   assigned different entity IDs, update the references in the package and card.
4. Enter the bean name and grinder setting as shown on the grinder, such as `4.0`. Optionally
   add a note. Press **Save entry**. Each save records a new timestamp, including when all three
   values are the same. You can also run **Coffee journal · Save entry** from
   **Developer tools → Actions**.

## View saved entries

The card shows the latest saved values separately from the editable fields. The latest entry
returns after a restart and the editable fields start empty. **Recent journal entries** shows
the past seven days, including saved values and notes. For a different period, open
**Activity** and select the journal's **Saved entries** sensor as the source. Earlier entries
are available within the configured retention period when the sensor is included in Recorder
and Activity.

For structured data, query the History REST endpoint with
`filter_entity_id=sensor.coffee_journal_last_saved&significant_changes_only=0`. With full
attributes, each save includes its full timestamp in `saved_at` and the bean name, grinder
setting and note in `entry`. Include **Last saved** in Recorder to query this history.
[Home Assistant History REST endpoint](https://contributors.home-assistant.io/core/internals/rest-api/#get-apihistoryperiodtimestamp)

## Add a journal for another machine

Copy the package to another file, such as `office_coffee_journal.yaml`. Before restarting, replace
every `coffee_journal` in that copy with a distinct prefix such as `office_coffee_journal`, and
change the display label `Coffee journal` to `Office coffee journal`. Give the second dashboard
card a matching title and replace the prefix in all its entity IDs and templates. Keep the
prefixes stable so each machine retains its saved entries.
