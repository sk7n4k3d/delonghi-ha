# Brew counts by drink

With read-only cloud telemetry enabled, Home Assistant can show how many times your machine has brewed a particular recipe and how many preparations it classifies as hot or cold. These counters share the existing brewing statistics request, normally refreshed about every 15 minutes.

## Choose the drinks on your dashboard

1. In **Settings → Devices & services**, open your De’Longhi My Coffee Lounge device. If needed, open the integration’s **Configure** dialog and enable **Read-only cloud telemetry**.
2. On the device page, expand the disabled entities. Enable the recipe counters you want to track. A recipe gets a sensor after the machine first reports a valid count, including zero. The hot and cold counters are enabled by default when reported.
3. Edit your dashboard, add an **Entities** card and select your counters. To see how many coffees you prepare per day or week, choose an individual counter as the source of a [Utility Meter](https://www.home-assistant.io/integrations/utility_meter/).

![Home Assistant showing three recipe counters and both temperature totals with sample counts](images/daedalus-drink-statistics.png)

## Reading the counters

Home Assistant uses verified drink names when it recognizes the product model. For other models, unmapped drinks, or older installations reporting only a numeric display-board identifier, the sensor shows the recipe ID, such as **Recipe 42 brew count**. See [verified recipe IDs by model](#verified-recipe-ids-by-model).

You can rename a sensor on its entity page. Its identity uses the machine and recipe ID, so adding a verified display name in a later release preserves the entity and its history.

The hot and cold totals come directly from the machine. A reported zero appears as **0**. If a previously reported counter disappears from the statistics, its sensor becomes **Unavailable** until a valid count returns. A lifetime count can include preparations made before Home Assistant was installed.

Home Assistant registers up to 256 distinct recipe counters per machine. Previously registered recipes keep their place, including recipes you have renamed or disabled.

## Verified recipe IDs by model

| Product model | Recipe ID | Drink |
| --- | ---: | --- |
| Eletta Ultra ECAM470.75.TXB | 7 | Cappuccino |
| Eletta Ultra ECAM470.75.TXB | 8 | Latte Macchiato |
| Eletta Ultra ECAM470.75.TXB | 29 | Café con Leche |
| Eletta Ultra ECAM470.75.TXB | 32 | Mild Filter |
| Eletta Ultra ECAM470.75.TXB | 33 | Caffè Crema |
| Eletta Ultra ECAM470.75.TXB | 35 | Black Eye |
| Eletta Ultra ECAM470.75.TXB | 52 | Iced Latte Macchiato |
| PrimaDonna Aromatic ECAM630.70 | 1 | Espresso |
| PrimaDonna Aromatic ECAM630.70 | 8 | Latte Macchiato |
| PrimaDonna Aromatic ECAM630.70 | 15 | Cappuccino Mix |

On the PrimaDonna Aromatic, one preparation of each listed drink increased its recipe
counter by one. The Latte Macchiato preparation also increased the machine’s brew
total by one. The Cappuccino Mix preparation increased the hot brew total by one.

The Eletta Ultra labels in the table also apply when Home Assistant shows **ECAM47070**. The owner of the verified ECAM470.75.TXB unit reported this product code after reconfiguring and confirmed that one Cappuccino advanced recipe 7 by one. [Owner’s observation](https://github.com/sk7n4k3d/delonghi-daedalus-ha/issues/8#issuecomment-7060227554)

To help verify another Eletta Ultra drink, claim an unmapped drink in the issue. Compare the counts in `BrewingStatisticsTotals` → `Recipes` before and after one normal brew. Report the product model, drink name, and recipe ID whose count increased by one. [Eletta Ultra recipe mapping discussion](https://github.com/sk7n4k3d/delonghi-daedalus-ha/issues/8#issuecomment-7051487885)

Add verified recipe names for another product model in a separate map alongside the Eletta Ultra map in the Daedalus sensor platform. Match the map to the product model code and test that the names only appear for that model. [Daedalus sensor platform](../custom_components/delonghi_daedalus/sensor.py)
