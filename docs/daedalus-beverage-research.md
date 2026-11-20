# My Coffee Lounge beverage protocol research

Static analysis of the shipped My Coffee Lounge Android 1.5.0 build 318 identifies the app's beverage command and response handling. The appliance of interest is the EU PrimaDonna Aromatic ECAM63070 with display and mainboard firmware 1.2.0 and Wi-Fi firmware 2.5.3. Brewing on that model and firmware has yet to be verified.

## Catalog and command

| Area | Evidence from the app |
|---|---|
| Ready to brew | The drinks loader requests model-, SKU-, locale- and firmware-specific basic recipes, then fetches drink images separately. It also loads custom drinks and a drink-visibility shadow. Lifetime brewing counters provide historical recipe IDs. |
| Brew | Flutter builds a `BeverageRequest` command with numeric `RecipeId` and `ProfileId`. The app has variants with `Custom`, `Save`, `StopStep`, `StopBrew` and options tied to the drink or rinse flow. The stop paths reuse the message with different flags. |
| Transport | Flutter passes the command map to the Android appliance kit through Pigeon. The kit assigns `RequestId` and selects the local WebSocket when available, adding its active `ConnectionId`. Otherwise the kit uses MQTT. |
| Acknowledgment | The plugin can return the assigned request ID immediately after dispatch. The app separately processes an appliance response correlated by `RequestId`, with `OK` and `KO` outcomes and an optional `ErrorDescription`. |
| Progress | The app follows status telemetry separately from acknowledgments. The progress handling reads `CurrMode`, `CurrFuncProgress`, `BeverageDispensing` and `Status`. |

## Reproducing the static findings

In the ARM64 split, `libapp.so` has SHA-256 `1361634e7d0a72572c5459eac68241e6572b960e96cf296b7a3063e42234f1c9` and combined Dart AOT snapshot hash `0451907c2eaa8467e848c0067bfe8ed4`. Recover the Dart ObjectPool and inspect its loads in the ARM64 disassembly. The catalog loader at `0xd1b7a0` calls the basic recipe and image request builders. The command builder at `0xd080c8` loads the beverage message, identifiers and flags before constructing the map. The reply handler at `0xd1fa84` reads `RequestId`, and the parser at `0xd1ff90` reads the response fields. The base APK's DEX classes `ConnectedApplianceApiImpl`, `AWSCloudApplianceConnection` and `WebSocketClient` show the plugin and transport behavior.

## ECAM63070 validation

On the ECAM63070, Home Assistant observed standby, initialization and idle during an attended wake. The app also handles remote control disabled, machine-only drinks and accessory conditions. Idle establishes the observed operating state. A given drink still requires its own catalog and readiness checks.

Useful passive evidence from normal use of an already-paired app would include the named drink and its catalog ID, confirmation of the selected profile slot, the transport used and the matching appliance response and progress transition. Redact the account, appliance identifiers, local addresses and authorization material. Preserve the distinction between dispatch, acknowledgment and the drink reaching completion. This evidence would establish which request variant and reply message the ECAM63070 actually uses. The app code alone leaves the model's valid recipe IDs, the reply message value, flag requirements and the effect of each accessory condition open.
