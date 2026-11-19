# My Coffee Lounge in Home Assistant

Install **De’Longhi My Coffee Lounge** from the [distribution repository](https://github.com/sk7n4k3d/delonghi-daedalus-ha).

- On Home Assistant **2026.11 and later**, open **Settings → Marketplace → Add from link** and add the distribution repository. The Marketplace requires a connected GitHub account for custom repositories and prompts you to connect if needed. Open the repository in Marketplace and select **Install**. [Home Assistant Marketplace documentation](https://www.home-assistant.io/integrations/marketplace/#custom-repositories)
- On earlier versions, open **HACS → Custom repositories**, add the distribution repository as an **Integration**, and download it from HACS.

Restart Home Assistant and open **Settings → Devices & services → Add integration**.

1. Sign in to the account used in My Coffee Lounge. Enable cloud telemetry to show the operating state, alarms and device capabilities reported by the machine, or to continue without local access.
2. Select the machine that belongs in Home Assistant. The picker includes machines the account can access. If the directory is temporarily unavailable, the setup flow offers manual entry.
3. Confirm the suggested LAN address using the appliance settings or your router's list of connected devices. Home Assistant learns the SHA-256 fingerprint from the certificate presented at that LAN address. First-use approval relies on the local network. You can compare the fingerprint with the certificate details in a desktop browser on the same network as a consistency check.
4. Approve the certificate only on a network you trust. On approval, Home Assistant pins the certificate and checks it before sending an account token to the LAN address. If you are unsure about the network or address, select **Use cloud telemetry only**. Open **Reconfigure** later to add local access.

Each machine has its own device page. Assign it to an area to keep the operating state, alarms and diagnostics together. Enable additional diagnostic entities on the device page when you need them.

### Upgrading an existing installation

Home Assistant keeps entity IDs, user names, and enabled or disabled choices for retained entities, including cloud entities whose fields are temporarily missing or whose telemetry is disabled. The upgrade removes cloud entities registered for private fields, including the obsolete raw cloud LAN address sensor. The dedicated **LAN IP address** sensor remains in **Diagnostic** with its existing settings. Update any automations or dashboards that reference the obsolete cloud sensor to use the dedicated sensor. The My Coffee Lounge machine name is now private and has been removed from cloud entity attributes.

Existing LAN configurations require certificate approval after upgrade. Open the integration entry’s menu and select **Reconfigure**. The same menu lets you change the LAN address without deleting the entry. Cloud telemetry can continue while local access is pending. If a previously approved certificate changes, investigate the change before accepting its replacement.

### Interpreting availability

The connectivity entity describes the authenticated local connection. Cloud telemetry uses the owner account and polls the reported state independently. The machine can remain connected to the cloud when Home Assistant loses its LAN route. A cloud outage can make status and alarm entities unavailable while local connectivity remains available.

When the device directory confirms the machine and omits its presence, Home Assistant shows the last reported cloud state. That state can stay the same across many polls while the machine is idle. Establish current connectivity and readiness separately before controlling the machine. When the directory reports the machine offline, Home Assistant withholds cloud status. If the directory is unavailable, Home Assistant uses the cloud status only when the timestamp for the `Status` field in `MachineStatus` is within 120 seconds of its clock in either direction.

Capabilities can still appear when cloud status is unavailable. Download the integration’s diagnostics and check `cloud_status_error` for the reason. If neither status nor capabilities are available, check `cloud_error`. Handle an unavailable operating state explicitly in automations.

Read-only monitoring has been exercised on the PrimaDonna Aromatic ECAM63070. The precise set of entities depends on the fields reported by each machine. For support, download the integration’s diagnostics from Home Assistant. Keep account details, certificate fingerprints and appliance labels out of public issue attachments.
