# Visible drinks and favorites

## Visible drinks

In **Settings → Devices & services**, open **Configure** for your My Coffee Lounge integration entry. Turn on **Read-only cloud telemetry** and **Show visible drinks count**. The **Visible drinks** sensor appears on the machine’s device page once Home Assistant receives a compatible visibility report. The EU PrimaDonna Aromatic ECAM63070 reports this data.

The count comes from the visibility flags in the machine’s last cloud report. It updates about every 15 minutes while cloud telemetry is working. If the visibility report is missing or unreadable, an existing sensor becomes unavailable and retries about once an hour. A valid report with every flag off shows zero.

Use My Coffee Lounge to see drink names for your machine and to check which drinks it can prepare with its current accessories. The sensor counts visibility flags. Home Assistant does not have a verified mapping from those flags to the app’s drink names.

## Keep favorites in Home Assistant

1. Open your machine in My Coffee Lounge and find the drinks you want to save.
2. In Home Assistant, open **Configure** on the integration entry for that machine. In **Favorite drink names**, type each name, then press **Enter** to add it. Save the options when you finish.
3. Open the machine’s device page and use **Favorite drink** to set your preferred drink. For dashboard access, add the entity to an **Entities** card.

Home Assistant remembers the list and selection for that machine. Choosing a favorite updates your local preference. Edit the list in **Configure** as your machine’s menu changes. Removing the selected name clears the preference, and saving an empty list removes the selector from the device page.

The favorites list works without cloud telemetry. It accepts up to 50 names of 255 characters each and treats names that differ only in case or surrounding spaces as duplicates. Users with access to the entity in Home Assistant can see the names.
