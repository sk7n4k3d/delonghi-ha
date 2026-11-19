# My Coffee Lounge in Home Assistant

Install **De’Longhi My Coffee Lounge** from the [distribution repository](https://github.com/sk7n4k3d/delonghi-daedalus-ha).

- On Home Assistant **2026.11 and later**, open **Settings → Marketplace → Add from link** and add the distribution repository. The Marketplace requires a connected GitHub account for custom repositories and prompts you to connect if needed. Open the repository in Marketplace and select **Install**. [Home Assistant Marketplace documentation](https://www.home-assistant.io/integrations/marketplace/#custom-repositories)
- On earlier versions, open **HACS → Custom repositories**, add the distribution repository as an **Integration**, and download it from HACS.

Restart Home Assistant and open **Settings → Devices & services → Add integration**.

1. Sign in to the account used in My Coffee Lounge. Enable cloud telemetry to show the operating state, alarms and device capabilities reported by the machine.
2. Select the machine that belongs in Home Assistant. The picker includes machines the account can access. If the directory is temporarily unavailable, the setup flow offers manual entry.
3. Review the suggested LAN address. Before approving local access, compare the displayed SHA-256 certificate fingerprint with the appliance certificate viewed independently from a trusted device on your home network. Use the address shown in the appliance settings or your router. In a desktop browser, open the appliance's HTTPS address and inspect the certificate details. You can inspect its certificate even if the browser warns that it is self-signed.
4. If the machine is away from the home network or you cannot verify its fingerprint, select **Use cloud telemetry only**. Open **Reconfigure** later to add local access.

Each machine has its own device page. Assign it to an area to keep the operating state, alarms and diagnostics together. Enable additional diagnostic entities on the device page when you need them.

### Upgrading an existing installation

Home Assistant keeps entity IDs, user names, and enabled or disabled choices for retained entities. The upgrade removes the obsolete raw cloud LAN address sensor. The dedicated **LAN IP address** sensor remains in **Diagnostic** with its existing settings. Update any automations or dashboards that reference the obsolete cloud sensor to use the dedicated sensor.

Existing LAN configurations require one certificate comparison after upgrade. Open the integration entry’s menu and select **Reconfigure**. The same menu lets you change the LAN address without deleting the entry. Cloud telemetry can continue during certificate verification. If a previously verified certificate changes, investigate the change before accepting its replacement.

### Interpreting availability

The connectivity entity describes the authenticated local connection. Cloud telemetry uses the owner account and polls the reported state independently. The machine can remain connected to the cloud when Home Assistant loses its LAN route. A cloud outage can make status and alarm entities unavailable while local connectivity remains available. An unavailable operating state should be handled explicitly in automations.

Read-only monitoring has been exercised on the PrimaDonna Aromatic ECAM63070. The precise set of entities depends on the fields reported by each machine. For support, download the integration’s diagnostics from Home Assistant. Keep account details, certificate fingerprints and appliance labels out of public issue attachments.
