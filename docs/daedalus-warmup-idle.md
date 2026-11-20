# Get an idle notification after warm-up

The blueprint sends a phone notification when Home Assistant observes the EU PrimaDonna
Aromatic ECAM630.70 move from **Initialization** to **Idle** after an acknowledged
**Warm up** request. The warm-up action supports UI and mainboard firmware 1.2.0 and Wi-Fi
firmware 2.5.3. The notification describes the reported operating state. Check the machine
before selecting or preparing a drink.

1. In **Settings → Devices & services**, open the **De’Longhi My Coffee Lounge** integration
   options for the machine. Turn on **Warm up** and **Cloud telemetry**.
2. Sign in to your Home Assistant server in the Companion app on the receiving phone.
   Allow notifications in the phone's settings. [Companion app notification setup](https://companion.home-assistant.io/docs/notifications/notifications-basic/)
3. [Import the warm-up idle blueprint](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fsk7n4k3d%2Fdelonghi-ha%2Fblob%2Fmaster%2Fblueprints%2Fautomation%2Fdaedalus-warmup-idle.yaml)
   and create an automation from it. Select the coffee machine and the Companion app phone,
   then save the automation. The blueprint uses Home Assistant 2024.11 or later and needs
   an integration version containing the warm-up idle event.
4. Keep the spouts clear for the hot-water rinse and attend the first run. Use the machine's
   **Warm up** button in Home Assistant, or call the **Warm up** action for the machine's
   configuration entry. Both use the same observation.

Home Assistant starts a brief read-only check for each acknowledged warm-up while an
automation is listening for that machine. It sends a notice only when it sees both stages within
four minutes of the request, with fresh status readings and clear reported alarms. This
window limits the status checks. A stage can pass between readings, and missing status or alarm
data, an offline machine, or a Home Assistant restart can leave the warm-up unannounced. The
automation responds to this Home Assistant action. Waking the appliance from its controls or
from My Coffee Lounge is outside that observation.

The blueprint only sends the phone notification. To stop receiving it, disable the automation.
