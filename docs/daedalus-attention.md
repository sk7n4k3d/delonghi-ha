# Coffee machine attention card

See current Daedalus alerts for one machine on a Home Assistant dashboard. The card groups
water, beans and grounds under **Act now** and shows descaling, cleaning requests and water
filter replacement under **Maintenance reminders**. It uses Home Assistant's built-in Markdown
card and reads the existing entity states without polling the machine.

## Add the card

1. Enable cloud telemetry for your [My Coffee Lounge integration](daedalus-setup.md).
2. On the machine's device page or in **Developer tools → States**, find the
   binary sensors your machine reports. Entity IDs depend on the machine name,
   Home Assistant language and entity registry. Use one machine's entities per card.
3. Edit the dashboard, add a **Manual** card, and paste
   [daedalus-attention-card.yaml](daedalus-attention-card.yaml). Replace the sample entity IDs
   in `alerts` with the actual IDs. Use `none` without quotes for any alert your machine does
   not expose or that you choose to omit. For another machine, copy the card, use that machine's
   entity IDs, and give the card a distinct title.

| Card item | Daedalus binary sensor (English name) |
| --- | --- |
| Refill the water tank | Low water level |
| Reinsert the water tank | Water tank missing |
| Refill the bean hopper | No coffee beans |
| Empty the grounds container | Grounds container full |
| Descale the machine | Descaling required |
| Check the machine for cleaning instructions | Cleaning required |
| Clean the brew unit | Brew unit cleaning required |
| Replace the water filter | Replace water filter |

Use **Low water level** for refill and **Water tank missing** for reinsertion. The separate
**Water tank level** and **Water tank present** entities report other signals.
**Cleaning required** is a general request to check the machine's display. On the PrimaDonna
Aromatic, it can appear when the hot milk carafe needs rinsing. The **Brew unit cleaning required**
sensor has its own reminder for the brew unit. The maintenance notification blueprint has an
optional input for each cleaning sensor. After updating the blueprint, edit your existing
automation and select **Cleaning required** in the new input to receive that alert.
[Maintenance notification blueprint](../blueprints/automation/daedalus-maintenance-notifications.yaml)

An active sensor appears in its alert group. A configured sensor without a usable reading
appears under **Status incomplete** as missing, unavailable or unknown, while available
alerts remain visible. The card reports no active alerts only when every configured sensor
is off. Sensors set to `none` stay outside that check, and a card with no selected sensors
reports that explicitly. The card displays what the integration currently reports. For details
on when the cloud state can be retained or withheld, see the availability notes.
[Interpreting availability](daedalus-setup.md#interpreting-availability)
