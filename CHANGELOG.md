# Changelog

All notable changes to this integration are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [1.9.11] — 2026-11-21

### Added

- **Le service dit ce qu'il vient de stocker pour rien (#92).** Un baseline égal
  ou inférieur à la valeur cloud de **sa propre clé** est inerte par
  construction : les capteurs exposent `max(cloud, baseline)`, l'entrée est donc
  enregistrée, horodatée, puis ignorée à chaque rafraîchissement. C'est
  exactement ce qui a laissé six mois un chiffre recopié sur le mauvais compteur
  (voir #90). La saisie reste **acceptée** — le cloud peut être gelé ou muet,
  c'est précisément le cas où le baseline sert — mais elle est désormais signalée
  dans le log (`warning`, la clé et les deux nombres) et dans le **retour de
  service** : `stored`, `effective`, `inert`, `not_checked`, `notes`. Le service
  est déclaré `SupportsResponse.OPTIONAL`, donc les appels qui ne demandent de
  retour (automatisations existantes) ne changent pas de comportement.
  Quand aucune valeur cloud n'est comparable — firmware qui n'expose pas la clé,
  cloud muet, ou valeur non numérique — **rien n'est revendiqué** : pas
  d'avertissement, et la note dit que le contrôle n'a pas pu être fait. Un faux
  « c'est inerte » aurait été pire que le silence qu'il remplace.
- **`espresso` et `total_espressos` ont enfin une aide qui les distingue.**
  `espresso` est le compteur d'**une** boisson (`d705_tot_id1_espr`),
  `total_espressos` le total de **toutes** les boissons espresso
  (`d704_tot_bev_espressi`). La phrase « set the same UI number here » ne peut
  plus se lire comme une invitation à remplir plusieurs champs avec le même
  chiffre : elle ne vise plus que le cas `total_beverages` ↔ `total_espressos`,
  et dit explicitement de ne pas y recopier le compteur d'une boisson.

## [1.9.10] — 2026-11-21

### Fixed

- **`baseline_age_days` rend un vrai `null` (#90).** En 1.9.9, un baseline sans
  date de saisie publiait la chaîne `"unknown"` dans `baseline_age_days`, un
  champ numérique. Un template qui teste `is none` tombait donc à l'envers : la
  chaîne est « vraie » en Jinja, et une comparaison arithmétique sur `"unknown"`
  lève une erreur au lieu de s'abstenir. Le champ vaut désormais `null` quand la
  date manque.
  `baseline_set_at` garde `"unknown"` : lui est lu sur la carte, c'est du texte.
  Le contraste des deux formes est maintenant commenté dans le code pour qu'on ne
  les « unifie » pas par réflexe.

## [1.9.9] — 2026-11-21

### Breaking changes

- **`reset_local_baseline` exige `confirm=true` (#90).** Le service supprimait
  d'un seul clic, sans avertissement, tous les baselines enregistrés localement.
  Sans `confirm`, l'appel ne modifie plus rien et renvoie à la place la liste des
  compteurs stockés avec leur date de saisie — le premier appel sert donc de
  prévisualisation. Les automatisations qui appelaient le service sans ce champ
  échoueront volontairement : chaque baseline est un nombre relevé sur l'écran de
  la machine, et le perdre en silence fait retomber le capteur sur une valeur cloud
  que le firmware ne met plus à jour.

### Added

- **Chaque baseline garde sa date de saisie (#90).** `set_baseline_from_screen`
  horodate désormais clé par clé. Les dates sont rangées dans une clé de
  métadonnées du même fichier de `.storage`, sans changement de
  `STORAGE_VERSION` : Home Assistant refuse un fichier dont il ne connaît pas la
  version, ce qui aurait effacé en silence les baselines déjà enregistrés. Une
  entrée antérieure à ce changement, ou dont la date est illisible, est annoncée
  `unknown` — jamais comme récente.
- **Le capteur dit ce qu'il masque (#90).** Quand un compteur est tenu par un
  baseline, ses attributs exposent `baseline_set_at`, `baseline_age_days`,
  `cloud_below_baseline_refreshes` et `baseline_stale`. Ce dernier passe à `true`
  après trois refresh complets (~30 min) pendant lesquels le cloud reste sous le
  baseline : un ou deux refresh sous une saisie manuelle est normal, puisque la
  saisie démarre volontairement devant le cloud ; ce qui ne l'est pas, c'est que
  le cloud ne rattrape jamais.

### Fixed

- **Un nombre saisi il y a des mois ne ressemble plus à une mesure à jour (#90).**
  Le firmware de l'Eletta Explore ne pousse les compteurs `d5XX`/`d7XX` au cloud
  que sur des événements rares (constaté sur le terrain : dernier push spontané le
  2026-04-08, ~140 boissons plus tôt). Le baseline manuel qui compense cela
  s'affichait sans ancienneté, si bien qu'un compteur figé depuis mai passait pour
  un compteur lent à se rafraîchir. La valeur affichée reste
  `max(cloud, baseline)` — le cloud reprend la main dès qu'il rattrape — mais elle
  est maintenant datée, et le capteur signale explicitement qu'il montre une saisie.

## [1.9.8] — 2026-11-21

### Fixed

- **Eletta Explore : `Brewing` dès la mouture (#89).** Le cycle espresso
  s'ouvre sur l'action 4 — la mouture — pendant que l'octet d'état est encore à
  `Ready` : le capteur restait donc ~5 s à `Ready` avant de basculer, alors que
  le café coulait. L'action 4 rejoint le jeu retenu par #88, qui devient
  `{4, 6, 7, 10, 11, 13, 14}` ; toutes les gardes de #88 restent en place (source
  cloud, fraîcheur ≤ 90 s, frame complète au bon préfixe magic, triple
  (état, action, progression) cohérent, progression entre 0 et 100 et
  CRC-16/SPI-FUJITSU vérifié), et le périmètre est toujours `DL-striker-cb`.
  Ce 4 est une **action**, à ne pas lire comme l'état 4 : dans `MACHINE_STATES`,
  le code d'état 4 est `Error`, et l'inférence ne s'applique qu'à une frame dont
  l'état est `Ready` — une frame d'erreur ne peut pas l'atteindre. Limite
  assumée : un seul expresso capturé n'écarte pas un cycle non-brew qui moudrait
  en restant à `Ready` (il s'afficherait `Brewing`) ; rinçage, détartrage et eau
  chaude sont bien exclus, chacun déclare son propre code d'état (8, 5, 11).

## [1.9.7] — 2026-11-21

### Fixed

- **Eletta Explore : l'état passe enfin à `Brewing` pendant l'extraction (#88, #83).**
  Sur le cloud, l'octet d'état ne bouge pas pendant un cycle espresso : la machine
  reste à `Ready` pendant que les octets de progression avancent. Le capteur d'état
  déduit maintenant `Brewing`, **à l'affichage uniquement**, quand la frame cloud
  complète valide le cycle : source cloud, fraîcheur ≤ 90 s, action dans le jeu
  observé `{6, 7, 10, 11, 13, 14}`, progression entre 0 et 100, préfixe magic de la
  frame, triple (état, action, progression) cohérent et CRC-16/SPI-FUJITSU vérifié.
  L'eau chaude et le rinçage gardent leur état explicite, la frame partagée avec
  l'action 6 étant départie par l'état machine. Périmètre assumé : `DL-striker-cb` ;
  `DL-striker-best`, autre Eletta Explore, reste hors champ faute de frames capturées
  sur ce firmware.

## [1.9.6] — 2026-11-21

### Added

- **Le brew se relance tout seul s'il est ignoré (#84).** Après une période
  d'inactivité, la machine avale parfois la première commande passée par le
  cloud : le 201 de l'API ne prouve que l'acceptation par le cloud, pas
  l'exécution (même pattern que #47 pour l'allumage — vérifié sur le terrain
  le 21/11, l'expresso n'est parti qu'à la deuxième pression). Après chaque
  envoi, l'intégration surveille désormais le moniteur brut pendant ~15 s :
  dès qu'un octet bouge, la commande est arrivée (pendant l'extraction les
  octets de progression avancent même si l'octet d'état reste à Ready, #83).
  Si rien ne bouge, la commande identique est renvoyée exactement une fois.
  Sans moniteur de référence, pas de verdict : jamais de renvoi à l'aveugle
  qui risquerait un double brew.

## [1.9.5] — 2026-11-21

### Fixed

- **Entités en pause plusieurs fois par jour (#73).** Le ping de réveil
  `ping_connected` est censé être best-effort, mais un timeout transport
  (`requests.RequestException`) lui échappait et remontait en `UpdateFailed` :
  toutes les entités passaient `unavailable` jusqu'au poll suivant, alors que
  la lecture d'état venait de réussir 80 ms avant. Le timeout est désormais
  absorbé comme n'importe quelle erreur de ping.
- **Le select de profil suit enfin la machine (#78).** Un seul résolveur fait
  loi : choix explicite dans HA → `active_profile` cloud → octet moniteur —
  plus jamais de repli silencieux sur le profil 1 au brew ou à la synchro de
  recettes (ces chemins exigent désormais un profil résolu, sinon erreur
  explicite). Le choix explicite est persisté : il survit aux redémarrages
  sans renvoi de commande. Et un `d286` gelé (fréquent sur Eletta Explore,
  prouvé sur le terrain) ne détourne plus la sélection : seul un vrai
  changement en session suit la machine.

### Changed

- **Octet d'action du brew restreint (#50).** Le contournement « action byte »
  n'est plus envoyé qu'aux firmwares où son efficacité est prouvée sur
  matériel (DL-millcore / PrimaDonna Soul ECAM610.75.MB, Espresso et Long
  Coffee validés), plus en cascade sur toute la gamme.

## [0.5.0] — Unreleased

### Breaking changes

- Daedalus requires an approved TLS certificate for local access. Existing entries must open **Reconfigure** and approve the certificate on a trusted network. Cloud telemetry continues for entries with it enabled, including during reconfiguration. LAN-only entries need certificate approval to restore connectivity, or can enable cloud telemetry in the integration options and select **Use cloud telemetry only** in **Reconfigure**. Reconfiguration keeps the paired machine's serial number and existing entity IDs.
- Previously registered cloud entities for private fields are removed, including the obsolete raw cloud LAN address sensor. Point any automations or dashboards that used that sensor to the dedicated **LAN IP address** diagnostic sensor.
- Cloud entities no longer expose the My Coffee Lounge machine name in their attributes. The entity registry keeps cloud entities when telemetry is disabled or a field is temporarily absent, preserving entity IDs and user settings when the data returns.

### Added

- **Eletta Ultra ECAM47070 recognised, Mild Filter counter named (#86).** The machine
  model gate was verified by running the real `machine_model_code`, not by reading it.
  The per-drink counter for the Mild Filter brew now carries its drink name instead of
  a bare slot number.
- **Aromatic: per-drink counters and hot-milk carafe rinse activity (#87).** Drink
  counters and the carafe rinse are now reported. The merged `is_on` property was
  replayed against 12 scenarios and matches the documented table on all 12; the
  `reported_field_timestamp` refactor does not regress `_recent_reported_status`.

### Known gaps

- The rinse sensor flips to `Unavailable` in the middle of a carafe rinse — a gap in
  the state window, not a failure to report.
- `CurrMode == 12` is still a bare magic number where the rest of the file uses named
  constants.
- #86 and #87 each introduced a drink-name table. They are folded into a single source
  so the recipe documentation cannot drift apart.

## [1.9.4] — 2026-11-19

### Fixed
- **LAN : la session rouvre enfin après un reload (cause racine #50).** Sur le
  firmware PrimaDonna Soul (DL-millcore), la méthode HTTP du `local_reg.json`
  est déterminante, vérifiée contre l'app Coffee Link décompilée : `POST`
  ouvre la session et déclenche l'échange de clés, `PUT` ne fait que
  keep-aliver une session existante. L'intégration n'envoyait qu'un seul `PUT`
  par démarrage : après chaque reload, « session creuse » — la machine gardait
  des clés devenues inutilisables de notre côté, plus jamais de handshake.
  Nouveau cycle de vie : `POST ?dsn=` initial, keep-alive `PUT` toutes 20 s,
  re-`POST` dès qu'une session devient stale, et `lan_active` ne passe à vrai
  qu'après un vrai handshake (le log « Registered — observe mode active »
  mentait et a orienté tout le debug à l'envers).
- **Cloud : le moniteur fantôme et les commandes gobées sur Soul.** Le
  `request_monitor` envoyait l'opcode `0x84` (« refresh app id ») ; c'est
  `0x75` qui force une publication fraîche sur millcore (doc protocole §7.3).
  L'annonce de session app sur Soul est un timestamp brut dans
  `device_connected` — l'intégration ne testait que `app_device_connected`
  (chemin Eletta, 404 sur Soul) et se désactivait **définitivement** au first
  404 ; les deux propriétés sont maintenant essayées, et l'échec backouffe
  1 h au lieu de verrouiller à vie.
- **Alignements handshake** (référence cremalink/ECAMpy) : `random_1` passé
  verbatim (plus de strip du padding `=`), `time_2` en millisecondes, champs
  `proto`/`ver`/`key_id`/`sec` journalisés (warning si secure-setup demandé).

### Added
- **Journal LAN complet** : access log aiohttp redirigé vers le logger du
  composant (les logs filtrés des testeurs ne jetaient plus les requêtes
  entrantes), chaque requête machine tracée avant toute vérification de
  session (une machine qui pollait sans session était invisible), corps et
  statut de chaque `local_reg` journalisés, route `commands.json` ouverte à
  tous les verbes (plus de 405 muets), et warning 30 s après un POST sans
  key exchange (piste slot de registration volé).

## [1.9.3] — 2026-11-18

### Changed
- **Suivi du profil en 60 s au lieu de 10 min** : la propriété cloud
  `d286_mach_sett_profile` (profil actif) est désormais lue à chaque poll
  léger, pas seulement au refresh complet. Un profil changé sur la machine
  est reflété par le select et le sensor dès le cycle suivant.

### Fixed
- **0xA9 : verdict terrain** — sur Eletta Explore (ECAM450.65.S), le paquet
  corrigé est accepté par le cloud mais ignoré par la machine : la
  sélection directe de profil est en réalité BLE-only. Documenté dans le
  code ; le profil continue de voyager dans chaque brew (fonctionne).


## [1.9.2] — 2026-11-18

### Fixed
- **Le select suit enfin la machine (F-COORD-01)** : un profil changé sur
  l'écran tactile est désormais reflété par l'entité select — le suivi se
  base sur la propriété cloud `active_profile` (autoritaire), jamais sur
  l'octet du monitor qui traîne et écrasait les choix HA (le bug
  d'origine). Une fenêtre de grâce de 90 s après une sélection depuis HA
  absorbe l'écho retardé de la propre commande.
- **Select de profil borné à 4** : le dropdown et le parsing de secours
  s'arrêtaient à « Profile 4 » alors que certaines machines (retour #50)
  déclarent 5 profils et que la commande accepte 1–5. Les deux chemins
  offrent/parsent maintenant 1–5.

## [1.9.1] — 2026-11-18

### Fixed
- **Confirmation de la mise sous tension (#47, Eletta Explore)** : après
  `switch.turn_on`, l'intégration attend désormais un monitor dont le
  timestamp (prouvé par CRC sur deux captures réelles) est postérieur à la
  commande ; si rien ne revient, POWER_ON est renvoyé à 20/45/75 s (la
  latence réellement observée chez le reporter était 70 s), et le switch
  reste `off` tant qu'aucune preuve fraîche n'est arrivée — plus d'état
  assumé après trois polls muets. Une notification persistante (auto-effacée
  dès que la machine répond, même tard) signale les commandes ignorées.
  Garde-fous de revue : `Error`/`Unknown (N)` ne comptent jamais comme
  confirmation ; une machine déjà allumée au dernier frame ne reçoit aucun
  renvoi parasite ; les modèles à moniteur cloud gelé sans session LAN
  gardent l'ancien contrat (état assumé + un retry à 180 s, pas de
  notification) ; chaque renvoi prend le verrou de commande, un `turn_off`
  en vol annule la ladder.

## [1.9.0] — 2026-11-18

### Added
- **État piloté par le LAN (#50, PrimaDonna Soul / DL-millcore)** : quand la
  machine pousse son moniteur par la connexion LAN (session vivante), c'est
  cette valeur fraîche qui fait `machine_state`, alarmes et profil — plus le
  moniteur cloud, figé tant que l'app Coffee Link est fermée sur ce
  firmware. L'overlay reste collant tant que le cloud ne bouge pas (pas de
  flicker Ready→Off entre deux pushes), et ne remplace jamais un cloud qui
  respire. `monitor_source` (lan/cloud) exposé dans les données.
- **Extraction routée par le LAN (#50)** : si la session LAN est vivante, la
  commande de boisson part par le socket LAN (double-shape firmware, comme
  power on/off) au lieu du `data_request` cloud que le firmware millcore
  encaisse sans réagir. Le pré-brew check utilise l'état LAN corrigé ; le
  fallback cloud reste la norme sur les autres modèles.
- **Sensor `API usage` (#45)** : compteur d'appels cloud dédié
  (`TOTAL_INCREASING`, catégorie diagnostic, désactivé par défaut) — les
  compteurs ne polluent plus les attributes du sensor d'état.

### Fixed
- **Une erreur réseau transitoire ne rend plus tout indisponible (#46)** :
  le plein refresh qui échoue est rejoué une fois après 2 s ; seules les
  erreurs persistantes ou d'auth remontent (les erreurs auth ne sont
  jamais retryées, le brew non plus — jamais de double extraction).
- **Alarmes d'un moniteur LAN jamais masquées (#50)** : le garde
  « monitor figé ⇒ alarmes supprimées » ne s'applique plus quand l'état
  vient du socket LAN.
- **Sélection de profil (#36)** : le paquet ECAM 0xA9 était mal framé
  (`0D 05 A9 <profil>` — le numéro de profil à la place des flags).
  Format corrigé `0D 06 A9 F0 <profil> CRC` conforme au framing utilisé par
  toutes les autres commandes. Si le Wi-Fi firmware ignore malgré tout cet
  opcode (documenté sur captures BLE), le profil continue de voyager dans
  l'octet profile_save de chaque brew.

## [0.4.7] — 2026-11-18

### Fixed / Added
- **Le SUBACK est attendu et journalisé avant l'envoi des get** (#18,
  remarque mimasek71 sur 0.4.6) : la ligne « subscribed N topics » était
  écrite avant la lecture du SUBACK, et les get partaient avant sa
  validation — l'absence d'erreur de refus ne prouvait donc pas que les
  abonnements avaient été accordés. Désormais : attente du SUBACK (10 s),
  journalisation debug de chaque code raison (`0x00` accordé, `0x87` non
  autorisé…), erreur explicite « no SUBACK received — the broker accepted
  the session but never acknowledged the subscriptions » s'il n'arrive
  jamais, et les get ne partent qu'après accord.

## [0.4.6] — 2026-11-18

### Added
- **Le refus d'abonnement cloud devient lisible** (#18, retest mimasek71
  0.4.5) : la session MQTT vérifie désormais les codes de réponse du SUBACK.
  Si le broker refuse tous les abonnements aux topics shadow (politique de
  l'auteurizeur accrochée au vrai nom de machine — nom de thing dérivé
  incorrect, par exemple), l'erreur le dit explicitement
  « broker refused all N shadow topic subscriptions (reason 0x87) — thing
  name or authorizer policy mismatch) » au lieu de patienter 20 s puis
  accuser les shadows. Un refus partiel est journalisé en warning.
- Les logs debug nomment le thing utilisé (masqué) et les topics réellement
  abonnés — un retest avec `custom_components.delonghi_daedalus: debug`
  devient auto-suffisant pour diagnostiquer à distance.

## [0.4.5] — 2026-11-18

### Fixed
- **La connexion cloud MQTT-over-WebSocket offre enfin les sous-protocoles
  MQTT** (`Sec-WebSocket-Protocol: mqttv5, mqtt`) : AWS IoT (port 443)
  répond HTTP 426 à toute montée en WebSocket qui ne les propose pas, ce
  qui rendait tous les capteurs cloud indisponibles même avec la télémétrie
  activée (#18, diagnostics mimasek71 en 0.4.4). Un test de régression
  imite la porte ATS : une montée sans sous-protocole est refusée 426.

## [0.4.4] — 2026-11-18

### Fixed
- **Télémétrie cloud : les shadows partiels sont conservés** (#18, retest
  mimasek71) : un shadow nommé qui ne répond ni en accepted ni en rejected
  (ex. `MachineCapabilities` absent sur certaines machines) ne fait plus
  échouer le fetch entier — les shadows bien reçus sont désormais exploités.
- **`cloud_error` n'est plus jamais vide** : les timeouts et les exceptions
  sans message produisent un texte descriptif (type d'erreur, rc CONNACK)
  au lieu de la chaîne vide observée dans les exports de diagnostic.
- **Le budget de connexion borne tout le handshake** (DNS + TLS + upgrade
  MQTT5), plus seulement la lecture du CONNACK.
- **Appels SSL bloquants supprimés de l'event loop** : le contexte de
  confiance était construit à chaque connexion (scan du magasin de certs
  signalé par HA) ; il est désormais bâti une fois hors de la boucle et caché.

### Added
- **Section `cloud` complète dans les diagnostics** : erreur sanitizée
  (JWT, mots de passe et numéros de série ne fuiteront jamais), catégorie
  de tri (auth, mqtt_connect, shadow_rejected, no_shadow, parse,
  device_lookup, timeout), codes structurés (rc CONNACK, statut HTTP),
  horodatage exact de la tentative échouée, topics abonnés (nom de thing
  masqué) et état de connexion cloud. Un retest à distance est désormais
  diagnostiquable sans accès à la machine.

## [0.4.3] — 2026-11-18

### Added
- **Options après installation** : la télémétrie cloud se bascule désormais
  via Configuration → intégration → Options (recharge automatique de
  l'entrée). Auparavant il fallait supprimer et réinstaller l'intégration
  pour l'activer (#18, stivxgamer).

### Fixed
- **Le fetch cloud ne bloque plus le refresh LAN** : la lecture des shadows
  tourne en tâche de fond (une à la fois, anti-recouvrement) ; un broker
  injoignable ne retarde plus l'état LAN de 75 s à chaque cycle.
- Clé de traduction morte `invalid_host` retirée ; `strings.json` ajouté
  (parité hassfest avec `translations/en.json`).

## [0.4.2] — 2026-11-18

### Fixed
- **Connexion cloud Eletta Ultra (#18, mimasek71)** : l'annuaire REST
  `/devices` utilisait un stage mort (HTTP 403) sans le header
  `source: mycoffeelounge` — basculé sur le stage qui répond, header
  ajouté, l'ancien stage gardé en fallback ; parsing des réponses groupées
  `ownedByMe`/`sharedToMe`.
- **MQTT shadow** : le broker ferme les connexions MQTT 3.1.1 à la
  authorizer custom — la connexion « app-style » (MQTT 5, path
  `/mqtt?x-amz-customauthorizer-name=dlg-prod-token-authorizer`, username
  de l'authorizer, JWT en password, client-id `dlg-appliance-kit-android-*`)
  est tentée en premier, les deux spellings 3.1.1 restent fallback. Codec
  MQTT 5 complet (sections Properties au bon ordre, spec §3.3.2), budget de
  connexion borné à 30 s pour rester sous le budget global du coordinator,
  timeouts REST qui jouent enfin le fallback de stage.

## [0.4.1] — 2026-11-14

### Changed
- **daedalus 0.4.1 — alias exacts d'après le dump brut de mimasek71 (#18)** :
  il a fourni les JSON `state.reported` anonymisés des deux shadows ; les
  noms réels (`Status`, `CurrMode`, `Filter`, `Clean`, `NoCoffee`, `Waste`,
  `FWUIVersion`, `Granulometries`, `SoundEnable`, `EnergySavingMode`,
  `DrinkClusters`, `Grinder`, `Hopper`, `GFXVersion`, `DrainWater`…)
  remplacent les alias devinés, conservés en secours pour les autres
  modèles.
- Nouveaux champs mappés : description d'étape, firmware Wi-Fi, version
  agent cloud, modèle carte mère, profils utilisateur (nombre + icônes),
  températures café, extinction auto, `accessories.None`/`Water` et
  `peripherals.TankPresence` (alias en chemin complet pour lever
  l'ambiguïté avec `alarms.TankPresence`).

### Security
- Les clés `BlePassCode` (code d'appairage BLE), `MAC` et `SN` ne sont
  **jamais** exposées en entités — le repli brut les aurait persistées dans
  la base HA et les diagnostics exportés.

## [0.4.0] — 2026-11-14

### Added
- **daedalus 0.4.0 — snapshot terrain Eletta Ultra intégré (mimasek71, #18)** :
  - `cloud_keys.py` : table de 77 champs cloud connus (état, alarmes,
    périphériques, accessoires, capacités) → noms lisibles traduits EN/FR.
    Les champs inconnus gardent le repli brut `Cloud <clé>` de la 0.3.0.
  - Les champs booléens mappés deviennent des `binary_sensor` (alarmes en
    `device_class=problem` : on = problème) au lieu de capteurs texte
    `true`/`false`.
  - Le shadow `MachineCapabilities` est désormais exposé (capteurs
    Diagnostic) — il était récupéré mais jeté depuis la 0.3.0.
  - Carte Lovelace compacte d'exemple : `docs/daedalus-cloud-card.yaml`.

### Changed
- Entités de télémétrie cloud existantes : renommage seul (unique_id
  inchangé) pour les champs reconnus ; les champs devenus `binary_sensor`
  changent de domaine — l'ancienne entité `sensor.cloud_*` correspondante
  est orpheline et retirée par HA.

## [1.8.0] — 2026-11-13

Support de la De'Longhi **Eletta Ultra** (pilet « Daedalus ») d'après le
retour terrain de @mimasek71 (ticket #18). Intégration `delonghi_daedalus`
passant à 0.3.0 ; `delonghi_coffee` inchangé à part le numéro de version.

### Added
- **Télémétrie cloud en lecture seule (expérimental, opt-in)** : case
  « Télémétrie cloud » à l'ajout de l'intégration. Résout le nom cloud de la
  machine via l'API De'Longhi, puis lit les shadows AWS IoT `MachineStatus`
  et `MachineCapabilities` (MQTT/WSS) toutes les 2 minutes. Les échecs cloud
  n'affectent jamais la connexion locale.
- Les clés connues du shadow d'état deviennent des entités `sensor`
  (valeurs brutes tant que les codes ne sont pas mappés).

### Changed
- **Connexion Gigya alignée sur l'app My Coffee Lounge 1.5.0** : `apiKey` en
  paramètre d'URL, `sessionExpiration=7776000`, JWT obtenu via
  `Authorization: Bearer` + secret de session. L'ancien format reste en
  repli automatique ; un refus d'identifiants n'est **jamais** rejoué.
- **Authentification locale** : les firmwares Eletta Ultra qui répondent
  `UNAUTHORIZED` à la trame `AUTH` obtiennent une reconnexion unique avec la
  trame minuscule `auth` ; l'orthographe retenue est mémorisée par machine.

### Fixed
- Le `sessionSecret` de session Gigya est conservé et utilisé pour la
  rotation du JWT (moins de redemandes de mot de passe).
- Durcissement complet (double revue) : masquage des secrets dans les
  messages d'erreur serveur, bornes strictes du parsing MQTT, budget global
  de 75 s pour le trajet cloud, nettoyage du coordinateur si la création des
  entités échoue.

## [1.7.1] — 2026-11-12

Correctif de packaging uniquement — aucun changement de code par rapport à 1.7.0.

### Fixed
- **Asset `delonghi_coffee.zip` de v1.7.0 mal empaqueté (#52)** : l'asset
  publié contenait les fichiers sous `custom_components/delonghi_coffee/`
  au lieu de la racine du zip, ce qui faisait échouer la mise à jour
  HACS / Marketplace (`no usable manifest.json`). L'asset avait été
  construit localement et avait remplacé celui de la CI, qui produit
  pourtant le bon layout plat. v1.7.1 est publié par le workflow
  `release.yaml` depuis le tag, sans retouche manuelle de l'asset.

## [1.7.0] — 2026-11-11

Correctifs des constats avérés du rapport blue-team 2026-11-05 (audit du
commit `f783725`, rapport `2026-11-05-delonghi-ha-f783725-BLUE-TEAM.md`).

### Fixed — sécurité (daedalus)
- **Escalade réauth déclenchée par une trame malformée**
  (`delonghi_daedalus/lan_protocol.py`, `api.py`) : tout `LanProtocolError`
  était classé « rejet d'auth », y compris une réponse non-JSON ou sans
  `ConnectionId` (machine en boot/upgrade, ou IP mal saisie sur laquelle un
  autre service écoute le port 443). Après une rotation JWT réussie côté
  cloud, la deuxième erreur ouvrait un flux `ConfigEntryAuthFailed` —
  saisie manuelle du mot de passe, sans auto-guérison. Seule la réponse
  explicite `Response != "OK"` (`LanAuthRejectedError`, nouvelle classe)
  escalade désormais ; les trames malformées restent transitoires et
  l'entrée se répare seule au cycle suivant.
- **DSN / e-mail / hôte LAN en clair dans les diagnostics**
  (les deux `diagnostics.py`) : `async_redact_data` masque par clés et ne
  peut rien contre la valeur free-text `coordinator.last_exception`, dans
  laquelle le texte d'erreur HTTP de `requests` embarque l'URL complète —
  DSN compris. Le champ est désormais purgé (`/dsns/<X>/`, JWT, e-mails,
  URL/IP LAN) et préfixé par le type d'exception pour le triage. Le texte
  des exceptions est aussi assaini **à la source** (`api.py`, `coordinator.py`),
  d'où il atteignait également les journaux ERROR de Home Assistant.
- **`host` et `serial_number` journalisés en clair au WARNING**
  (`delonghi_daedalus/config_flow.py`) : masquage partiel (`192.168.x.x`,
  4 derniers caractères du numéro de série) aligné sur la politique
  `REDACT_KEYS` des diagnostics — même canal public.
- **Masqueur d'e-mail divergents entre les deux intégrations** : la règle
  masquait le seul premier label du domaine et laissait survivre le label
  de l'organisation (`mail.entreprise.fr` → `m***.entreprise.fr`). Tous les
  labels sauf le dernier sont maintenant remplacés ; l'entrée du masque ne
  plante plus sur une valeur non-`str` ; le domaine sans point n'est plus
  un no-op silencieux. Un unique implémenteur par composant
  (`delonghi_daedalus/redact.py`, partagé flow/diagnostics).

### Fixed — sécurité (coffee)
- **`native_value` retournait une chaîne** avec `state_class` posé : le
  ValueMapper de Home Assistant rejette toute valeur non numérique dès
  qu'une unité/`state_class` est présente — le `TypeError` d'hier était
  remplacé par un `ValueError` levé par HA à chaque écriture d'état
  (le commentaire du fix promettait un affichage verbatim impossible).
  Le capteur passe désormais en `unknown` (valeur `None`) avec un
  avertissement unique, et redevient numérique dès que la machine renvoie
  un nombre.
- **Masquage devenus inductifs** (`delonghi_daedalus`/`logger.py`) : un
  `logging.Filter` appliqué au logger de chaque module masque désormais
  le message **rendu** (pas seulement l'argument), y compris les
  tracebacks — les lignes ajoutées plus tard sont couvertes sans rien
  faire. Le motif `auth_token` couvre la forme `auth_token=<valeur>` et
  les URL `/dsns/<X>/` sont purgées.
- **`config_entry` explicite** dans les deux `DataUpdateCoordinator` :
  l'escalade réauth ne dépend plus du ContextVar interne `current_entry`
  que HA déconseille aux custom integrations.
- Gigya : le texte `errorMessage` (contrôlé par le serveur, peut echoit le
  loginID) est assaini avant d'entrer dans l'exception `DeLonghiAuthError`.

Correctifs issus de l'audit complet du dépôt (lecture ligne à ligne des deux
intégrations + revue sécurité).

### Fixed — confidentialité
- **Adresse e-mail de l'utilisateur en clair dans les journaux Home Assistant**
  (`delonghi_coffee/api.py`) : `authenticate()` journalisait l'e-mail non masqué
  à chaque tentative (DEBUG) et à chaque échec Gigya (ERROR, visible sans mode
  debug). `logger.sanitize()` portait déjà le masqueur d'e-mail sans jamais
  être appliqué à ces trois lignes. Même fuite corrigée dans la
  `delonghi_daedalus/config_flow.py` (message d'erreur `all_pools`), masquée
  via un `_mask_email` local. Les journaux HA étant collés tels quels dans les
  tickets GitHub, c'était une fuite d'identifiant personnel pour tout le monde.

### Fixed — entités
- **Compteurs non numériques : entité cassée à chaque écriture d'état**
  (`delonghi_coffee/sensor.py`) : `parse_counters` conserve en chaîne brute une
  valeur de compteur qu'il ne peut pas convertir en `int` ; trois sites
  appliquaient ensuite une arithmétique sans garde — échelle
  `str * 0.001` dans `native_value` et dans `extra_state_attributes`, et
  comparaison `str < int` pour la source baseline/cloud (la garde existait déjà
  dans `LocalBaselineStore.merge`, pas dans ces deux-là). Les valeurs non
  numériques passent désormais brutes, la source affichée est `baseline`.

### Fixed — daedalus
- **Rotation JWT refusée deux fois : boucle `UpdateFailed` sans flux de
  réauthentification** (`delonghi_daedalus/coordinator.py`) : si la machine
  rejetait aussi le JWT fraîchement rotatif, l'erreur remontait en échec
  transitoire et le flux de réauthentification documenté ne se déclenchait
  jamais. Escalade maintenant en `ConfigEntryAuthFailed` ; les erreurs
  transport restent transitoires.

### Fixed — ressources
- **Fuite de session HTTP dans le parcours de configuration**
  (`delonghi_coffee/config_flow.py`) : la `requests.Session` créée par étape
  utilisateur/réauthentification n'était fermée sur aucun chemin (chaque
  tentative d'ajout laissait un pool keep-alive en suspens). Les deux étapes
  ferment désormais la session dans `finally`.

### Tests
- 12 tests de régression ajoutés (971 au total) : masque d'e-mail dans les
  journaux d'authentification, compteurs-chaînes (échelle + source baseline),
  fermeture de session dans les parcours, escalade de réauthentification JWT
  daedalus.

## [1.6.0] — 2026-10-29

Première version stable de la branche 1.6. Contenu = `1.6.0-beta.23` (passage en revue
des bugs + réactivité de l'API) plus la robustesse du chargement du catalogue.

### Fixed
- **Catalogue `ContentStack` injoignable : retries en explosion exponentielle.** Le
  CMS (`eu-cdn.contentstack.com`, bloqué par un filtre DNS le 2026-10-19) était
  ré-interrogé à chaque rafraîchissement complet et journalisait un traceback complet
  toutes les 10 minutes, indéfiniment. La fenêtre de retry double à chaque échec
  (plafonnée à 6 h), la traceback n'est plus journalisée qu'au premier échec, puis
  simples avertissements d'une ligne, avec une ligne de reprise. Les chemins de
  préparation sont intouchés : le catalogue est auxiliaire, les extractions utilisent
  les recettes de la machine.

## [1.6.0-beta.23] — 2026-10-15

### Fixed — issues
- **#42 — `Unknown (10)` pendant le fonctionnement:** `MACHINE_STATES`
  ne cartographiait que les codes d'état bruts 0-9. Les codes de phase
  de distribution (10-17) observés sur Eletta Explore sont maintenant
  nommés (`Preparing Milk`, `Dispensing Hot Water`, `Cleaning Milk
  System`, `Rinsing Water Filter`, `Preparing Chocolate`,
  `Preparing Milk Alt` — nomenclature du profil moniteur cremalink
  ECAM452, même famille de protocole, même position d'octet) et
  rejoignent `ACTIVE_MACHINE_STATES` pour le polling adaptatif.
- Le capteur d'état expose désormais `monitor_action` (octet 10) et
  `monitor_progress` (octet 11) en attributs : ce que la machine est en
  train de faire et où elle en est, visibles même quand l'octet d'état
  reste générique.

## [1.6.0-beta.21] — 2026-10-03

Deep bug-hunt pass + API reactivity overhaul. Every fix below is covered by
the test suite (948 tests green).

### Fixed — issues
- **#36 — profile select never changed the machine's active profile:**
  `async_select_option` only updated local state; no ECAM command was ever
  sent. It now sends ECAM `0xA9` (ProfileSelection, confirmed against the
  longshot protocol map: `ProfileSelection = 169`) and the old
  `sync_recipes` was renamed `set_active_profile` (recipe upload is a side
  effect of profile selection, not the primary opcode). The legacy
  `sync_recipes` button/service keep working as an alias. On command
  failure the select keeps the previous profile instead of lying.
- **#34 — DL-millcore (PrimaDonna Soul, late production runs) power/brew
  commands accepted by the cloud but ignored by the machine:** the model
  fell through the cmd-property routing (matched neither `DL-pd-*` nor
  `DL-striker-*`) and the attempt order tried the app_id packet first —
  the cloud answers HTTP 201 for both shapes, so the working no-app_id
  variant was never reached. `DL-millcore` now routes like `DL-pd-*`, and
  the `data_request` attempts try **without app_id first**.

### Fixed — critical (deep audit)
- **Reauth never triggered:** `DeLonghiAuthError` was mapped to
  `UpdateFailed` in the coordinator, so an expired/changed password looped
  forever with `last_update_success=False` and HA never opened the reauth
  flow. Now raises `ConfigEntryAuthFailed`.
- **Second machine on the same account impossible:** the config flow set
  `unique_id = email` and aborted, so multi-machine accounts (Eletta +
  PrimaDonna) could never add a second entry. The unique_id is now the
  device DSN.

### Fixed — major (deep audit)
- Socket leak on every reload: `requests.Session` was never closed;
  `api.close()` is now called from `async_unload_entry`.
- DSN leaked in shared diagnostics: the redaction list had
  `device_serial` but the entry key is `dsn`.
- Cloud outages were invisible: `get_status` swallowed transport errors,
  so light polls kept `last_update_success=True` with stale data; network
  failures now propagate to `UpdateFailed`.
- ContentStack catalog permanently disabled by one transient network
  failure: fetches now signal failure distinctly from empty, and the
  coordinator retries on the next full refresh.
- Coffee bean/drink catalog silently truncated at 100 entries: the
  prod_drink fetch now paginates.

### Fixed — minor (deep audit)
- `brew_custom` beverage mapping diverged from `BEVERAGES` (missing most
  drinks, `coffee` vs `regular` mismatch) — now derived from the single
  source of truth.
- Bit 15 "Coffee Beans Empty 2" now blocking everywhere (the pre-brew
  check blocked it but the switch didn't warn).
- TranscodeTable/model identification was dead code (never called);
  now runs once at the first full refresh (on the executor — HA strict
  mode rejects blocking HTTP in the event loop).
- `set_baseline_from_screen` no longer warns about the injected
  `config_entry_id` field.
- Switch staleness sentinel used `id()` (values recycled after GC);
  now holds the previous data object for exact identity comparison.
- LAN datapoints received are now merged into `coordinator.data` instead
  of being stored where no sensor could read them.

### Changed — API reactivity (the "it lags" pass)
- **Adaptive polling:** while the machine reports a transient state
  (Turning On, Brewing, Rinsing, Descaling, Heating…) the coordinator
  automatically shortens its interval from 60s to 10s, and reverts when
  the machine settles. State changes made on the machine's own touchscreen
  now surface in near-real-time too, not just HA-triggered ones.
- **Immediate refresh after every command:** new coordinator helper
  `async_refresh_after_command()` (instant refresh + 90s fast-poll) is
  wired into power on/off, profile select, brew buttons, cancel and bean
  profile selection. Ayla's HTTP 201 means "cloud accepted", not "machine
  received" — the machine's response now shows up within seconds.
- HTTP read timeout reduced 15s → 8s: a lagging cloud fails fast into
  the retry path instead of stalling the refresh chain.

## [1.6.0-beta.20] — 2026-08-26

Investigation pass on issue #23 (PrimaDonna Soul power/brew reported not
working) + supply-chain hardening. The LAN fix below is a **candidate**
root cause — not confirmed against real device logs, since no reporter has
provided diagnostics yet.

### Fixed
- **LAN session zombie (candidate root cause for #23):** `send_command_lan`
  trusted any LAN session ever established by a handshake, even if the
  device had gone silent for hours (WiFi drop, reboot, falling back to
  cloud-only). Commands were queued into a dead session, the queue never
  drained, and the cloud fallback in `switch.py` never fired because
  `send_command_lan` reported success — so power on/off could silently go
  nowhere on models with LAN enabled (PrimaDonna Soul family). The LAN
  server now tracks last device activity (handshake + command poll) and a
  session must have been confirmed within 30s to be trusted; otherwise the
  command falls back to cloud.
- **Pre-brew safety-check visibility (F-API-05):** `_pre_brew_check` skipped
  its machine-state/alarm checks silently when the cloud status couldn't be
  read — either via a caught exception (logged at DEBUG) or, more often,
  via `get_status()` swallowing its own errors and returning
  `machine_state="Unknown"` without raising at all (the except branch never
  saw it). Both paths now log at WARNING. Behaviour is unchanged (still
  sends the command — the firmware remains the real last-resort guard),
  only visibility improves. Targets the "nothing happens when I brew, no
  useful logs" pattern reported in #23.
- **Concurrent 401 re-authentication (F-API-04):** `_reauthenticating` was a
  plain bool checked-then-set with no lock. Several executor threads (poll
  + button press + switch toggle can all fire concurrently) hitting 401 on
  the same stale token could all call `authenticate()` at once, racing on
  session state. Now serialized on the existing token lock, with a 10s dedup
  window so a thread that acquires the lock right after another just
  refreshed doesn't redundantly re-authenticate and burn already
  rate-limited Ayla/Gigya quota (issue #18).

### Security
- **CI supply chain (F-CI-03):** all GitHub Actions across the six
  workflows are now pinned to commit SHA instead of mutable version tags
  (`checkout`, `setup-python`, `upload-artifact`, `action-gh-release`,
  `ruff-action`, `hacs/action`) — `hassfest` was already pinned this way.
  Each pin keeps its resolved tag as a trailing comment.

## [1.6.0-beta.19] — 2026-06-06

Audit-driven fixes. Headline: multi-appliance accounts (issue #30).

### Fixed
- **Device selection (#30):** the config flow no longer binds blindly to
  `devices[0]`. Accounts that also hold a non-coffee De'Longhi appliance
  (e.g. a Pinguino air conditioner, `oem_model=DL-pac`) were being bound to
  the wrong DSN, so every command returned `HTTP 404` on
  `app_data_request/datapoints.json`. The flow now filters on the known
  coffee-machine OEM prefixes (`DL-striker-`, `DL-pd-`, `DL-dinamica-`,
  `DL-maestosa-`, `DL-millcore`), shows a picker when an account has more
  than one coffee machine, and reports `no_coffee_machine` when none is
  found. `get_devices()` likewise derives device metadata / oem backfill
  from a coffee device, not the first arbitrary one.
- **Profile selection no longer reverts:** the coordinator was overwriting
  the user-selected profile with the monitor's `profile` byte on every poll,
  silently reverting the profile-select dropdown within 60s. It is now only
  seeded once (first refresh).
- **Fast-poll after brew:** pressing a brew button now forces an immediate
  refresh so the 5s fast-poll window takes effect right away instead of after
  the already-armed 60s timer — the transient Brewing/Frothing states are
  actually captured.
- **Retry storms:** `cancel_brew`, `sync_recipes` and the bean-system
  commands no longer stack a second `@_retry` on top of `send_command`
  (could fire up to 9 POSTs and replay commands). All-404 now raises a
  dedicated `DeLonghiUnsupportedError` that the retry decorator treats as
  definitive.
- **Token TTL clamp:** a short/absurd `expires_in` from Ayla is clamped to a
  600s floor to avoid a re-auth-on-every-request storm.
- **Cancel / brew button availability:** cancel is now offered during all
  cancellable busy states (Rinsing/Heating/Descaling/Turning On), not just
  the narrow Brewing window; the dead `"Sleep"` guard now matches the real
  `"Going to sleep"` state name.

### Changed
- Bumped `requests` floor to `>=2.32.0` (CVE-2023-32681, CVE-2024-35195).

## [1.6.0-beta.18] — 2026-05-14

Ships a fallback for the Eletta Explore firmware bug where the cumulative
counter family (``d5XX``/``d7XX``) stops being pushed to the Ayla cloud
after the boot that follows a re-provision event. Diagnosed live on
AC000W038925641: counters frozen at ``2026-04-08T04:17:30Z`` (post
WAN-swap re-provision) while the touchscreen showed +140 brews,
+1 descale and +1 filter replacement. None of LAN mode, output-property
writes, or BLE-catalogue ECAM opcodes (``ParameterRead`` 0x95,
``ParameterReadExt`` 0xA1, ``StatisticsRead`` 0xA2) brought the firmware
back to life on this build — confirmed with the machine awake at
``state=Ready``.

### Added (delonghi_coffee)

- **``LocalBaselineStore``** (new ``local_baseline.py``) — per-DSN
  persistent override for counter sensors backed by HA's ``Store`` helper.
  Stores integer counter values keyed by ``parse_counters()`` names
  (``total_espressos``, ``descale_count``, ``filter_replacements``,
  ``total_water_ml``, …). Loaded lazily on the first coordinator refresh.

- **Counter sensors expose ``max(cloud, baseline)``**
  (``DeLonghiCounterSensor.native_value``). The cloud wins as soon as
  it pushes a higher value, preserving ``TOTAL_INCREASING`` semantics —
  the baseline is invisible until the user opts in. New
  ``extra_state_attributes`` (``cloud_value``, ``baseline_value``,
  ``source``) appear only when an override is in effect, keeping the
  entity card clean for everyone else.

- **Service ``delonghi_coffee.set_baseline_from_screen``** — maps the
  touchscreen field labels (``Tot. seulement café``, ``Tot. détartrages``,
  ``Tot. filtres``, ``Tot. litres eau``, …) to counter keys, writes them
  through the store and requests a coordinator refresh so sensors update
  immediately instead of waiting for the 60s poll. Litres are converted
  to internal ml (the sensor reapplies ``scale=0.001`` on read).

- **Service ``delonghi_coffee.reset_local_baseline``** — drops every
  override and reverts sensors to whatever the cloud reports. Useful
  once the firmware finally pushes a fresh boot snapshot.

### Investigation notes

The full diagnostic trail (LAN port 10275 closed, ``lan_enabled=false``
cloud-side, idempotent writes accepted by Ayla but ignored by firmware,
BLE-catalogue read opcodes silently dropped, ``app_data_response``
frozen) is captured in the module docstring of ``local_baseline.py`` so
future contributors don't re-walk the same path.

## [1.6.0-beta.17] — 2026-05-08

Fixes the most impactful HIGH findings from the multi-agent security +
correctness audit run on master @ 0a0e557. Six audit agents in parallel
returned 1 critical bug + 16 HIGH + ~35 MEDIUM + ~52 LOW; this release
ships the four fix clusters that affect real users: switch state machine
correctness, brew-recipe coverage, daedalus auth surface, and LAN crypto
hardening.

### Fixed (delonghi_coffee)

- **Power switch state machine — `is_on` was not a pure read** (B1, the
  one critical bug from the audit). HA reads ``is_on`` multiple times
  per coordinator refresh (renderers, state-class evaluation, automations)
  and the original property body advanced ``_monitor_stale_count``,
  cleared ``_last_commanded_on``, and emitted the
  ``Switch: monitor confirmed Off state`` log line on every spurious
  read. Move the staleness state machine into a new
  ``_reconcile_monitor_state()`` helper invoked exactly once per refresh
  by ``_handle_coordinator_update()``. The property now reads a cached
  ``_cached_is_on`` with zero side effects. A sentinel
  (``id(coordinator.data)``) protects against test fixtures that swap
  the data dict without firing the coordinator hook.

- **`async_turn_off` could not abort an in-progress `async_turn_on`
  during the 15s wake delay** (H-logic-2). Both methods shared a single
  ``_cmd_lock``: turn_off saw ``_cmd_lock.locked() → True`` and returned
  silently with the misleading log ``Power command already in progress,
  ignoring``. The machine warmed up uselessly and the 3-min retry then
  fired POWER_ON. Fix: the wake now awaits a cooperative
  ``self._cancel_turn_on`` event with the same timeout; turn_off sets
  the event before awaiting the lock, turn_on returns early without
  sending POWER_ON, lock releases, turn_off proceeds with POWER_OFF.

- **Iced beverage detection missed `brew_over_ice` AND the entire
  `mug_i_*` family** (H-logic-3). The original ``startswith(("i_",
  "mi_", "over_ice"))`` did not match ``brew_over_ice`` (drink_id 27 —
  starts with ``brew_``) nor the Mug Cold family (drink_ids 100-107 —
  starts with ``mug_i_``). The recipe-to-brew path then dropped the
  ICED(31)=0 marker for those drinks and kept the hot-recipe
  coffee/milk/hot-water quantities — the machine brewed hot drinks
  instead of pouring over ice.

- **Bean Adapt parser silently collapsed local + English names**
  (H-logic-1). The two 20-byte UTF-16-BE name slots in
  ``d{N}_beansystem_{i}`` were decoded by passing the whole 40+ byte
  blob to ``_decode_utf16``, which (correctly hardened in beta.14)
  truncated at the first 2-byte NUL pair. The consumer's
  ``text.split('\\x00')`` then never split, so the English variant
  always equalled the local one. Fix slices the two halves explicitly
  and decodes each. Issue #7's intended UX (local + English alongside)
  was silently broken since beta.14.

- **`_retry` decorator suppressed re-auth failures** (H-coffee-3). When
  re-authentication itself raised ``DeLonghiAuthError`` (Gigya rejected
  the credential — e.g. password actually wrong) or
  ``DeLonghiApiError`` (Gigya transient 502), the decorator's
  ``contextlib.suppress(DeLonghiAuthError, DeLonghiApiError)`` ate the
  signal silently, the loop fell through, retried two more times
  against the same dead credentials, and surfaced the generic
  ``"function failed after 3 attempts"`` message. The coordinator
  mapped that to ``UpdateFailed`` (transient) and HA never prompted
  the user for reauth despite the password being wrong. Fix
  re-raises the auth/api error so the coordinator can translate to
  ``ConfigEntryAuthFailed`` and trigger the reauth flow.

### Fixed (delonghi_daedalus)

- **Diagnostics dump leaked the serial number through `machine_name`**
  (H-daedalus-3). The config flow stores the SN under both
  ``serial_number`` AND ``CONF_MACHINE_NAME``; only the former was in
  ``REDACT_KEYS``. Added ``machine_name`` to the redaction list.

- **Network failures during LAN AUTH were misclassified as auth
  failures** (H-daedalus-2). ``connect_lan`` wrapped both
  ``aiohttp.ClientError`` (transport: TLS abort, peer reset, read
  timeout) and ``LanProtocolError`` (device explicitly rejected our
  frame) as ``DaedalusAuthError``. The coordinator interpreted any
  ``DaedalusAuthError`` as "JWT stale, refresh via Gigya" — turning a
  flaky Wi-Fi link into an unbounded Gigya ``getJWT`` storm. Fix
  splits transport errors (now ``DaedalusConnectionError``, no Gigya
  call) from auth-side rejections (still ``DaedalusAuthError``).

- **`send_command` did not catch the reserved-key guard** (H-daedalus-1,
  defense-in-depth). ``build_command_frame`` raises
  ``LanProtocolError`` when caller-supplied ``params`` collide with
  ``Message`` / ``ConnectionId`` / ``RequestId``; the wrapper only
  caught ``aiohttp.ClientError`` so a future service handler that
  accepts user input could surface an opaque error and lose the
  request_id mapping. Wire-format violations now raise
  ``DaedalusError`` distinct from ``DaedalusConnectionError``.

### Added (delonghi_daedalus)

- **Startup self-check on Gigya apiKey lengths**
  (``entry._validate_apikey_lengths``). Logs an ERROR if any pool's
  ``GIGYA_API_KEYS[pool]`` value differs from the canonical length
  extracted from the My Coffee Lounge APK manifest (24/66/66). Live
  ticket Issue #18 (stivxgamer): apiKey truncation on the HACS mirror
  side is the working hypothesis; this check turns the corruption from
  silent into actionable.

- **400093 error messages now include the apiKey fingerprint**
  (``len:N sha1[:8]=<hex>`` — non-secret, the apiKey itself ships in
  the public APK manifest). Lets a remote bug report disambiguate
  "config corrupted on this install" from "Gigya transient blip"
  without asking the user to paste their apiKey verbatim.

### Fixed (LAN protocol hardening, delonghi_coffee/lan.py)

- **Anti-replay handshake ratchet is now per-peer** (H-coffee-4).
  ``_last_handshake_time1`` was a single global integer; a rogue peer
  inside the allowlist (ARP spoofing, misconfigured 2nd HA install)
  could send ``time_1 = 2**31 - 1`` to pin the floor at int max —
  the legitimate device's smaller ``time_1`` was permanently rejected
  as 'stale'. Per-peer dict (keyed by IP) localises the blast radius.

- **CBC IV mutations now serialised under the server lock** (H-coffee-1).
  ``_handle_command_poll`` and ``_handle_property_push`` released the
  lock before ``encrypt_app_to_device`` / ``decrypt_device_to_app``,
  which mutate ``session.app_iv`` / ``session.dev_iv`` in-place. Two
  concurrent polls/pushes could capture the same IV, encrypt
  different plaintexts under the same key+IV (CBC IV reuse leaks
  plaintext XOR for block-coincident prefixes), and the chain
  desynced from the device. Encrypt/decrypt now stay inside the
  ``async with self._lock`` block.

### Internals

- ``manifest.json`` (delonghi_coffee): cryptography minimum bumped
  ``>=41.0.0 → >=42.0.0`` to dodge CVE-2023-50782 (un-exploitable in
  this codebase but hygiene).
- ``manifest.json`` (delonghi_daedalus): aiohttp minimum bumped
  ``>=3.9.0 → >=3.10.0`` to dodge CVE-2024-23334 (path traversal,
  fixed in 3.9.2; 3.10+ further hardening).
- delonghi_daedalus version bumped ``0.2.0 → 0.2.1`` to mirror the
  fixes shipped to the mirror repo.

## [1.6.0-beta.16] — 2026-05-08

### Fixed

- **`Switch: monitor confirmed Off state` logged after a turn_off that
  never reached the machine**. Live trace 2026-05-08 on Bastien's Eletta
  Explore: `cloud=SYNC` (MQTT keepalive dead, machine WiFi module
  zombie), monitor frozen 961 min on `machine_state="Ready"`. Coordinator
  was rebranding `Ready → Off` purely on monitor staleness, regardless of
  cloud session health. A `turn_off` request fired POWER_OFF (HTTP 201
  accepted by Ayla cloud, never relayed via MQTT), the next refresh
  matched the fabricated "Off" against `_last_commanded_on=False`, and
  the switch logged `monitor confirmed Off state` — a flat lie, the
  machine was still physically running. Fix gates the "Off" rebrand on
  `cloud_status == "RUN"`. With `cloud=SYNC` the last known monitor
  ("Ready" frozen pre-zombie) wins, and the switch's existing
  `_monitor_stale_count` machinery correctly flips to `assumed_state`
  after 3 polls instead of inventing a confirmation. Locked by 2 new
  regression tests in `TestMonitorStaleness`:
  `test_stale_monitor_with_cloud_sync_does_not_force_off` (the fix, with
  the quoted live monitor raw for traceability) +
  `test_stale_monitor_with_cloud_run_still_forces_off` (explicit
  positive case for the gate).

## [1.6.0-beta.15] — 2026-05-08

### Fixed

- **Firmware recipe templates leaked into beverage discovery**. The cloud
  surfaces `default`, `default_1..7` and `bs_recipe` under the same
  `dXXX_rec_<key>` naming as real drinks, so `parse_available_beverages`
  added them to `coordinator.beverages` and the button platform created
  phantom brew buttons for them. Pressing one would fire `brew_beverage`
  against a template payload — undefined firmware behaviour (silent
  reject, off-recipe brew, or service mode entry). Side effect: removes
  the recurring `Unknown beverage keys [...]` log warning several testers
  (Bastien on Eletta Explore, @JanKraslice on ECAM610.74) hit on every
  full refresh. New `const.TEMPLATE_BEVERAGE_KEYS` exposes the canonical
  set so other surfaces (diagnostics, button platform warnings) can
  reuse it without duplicating the list. Filter is exact-match — a real
  drink starting with `default` (none today, hedging) is not falsely
  excluded. Locked by 7 regression tests in
  `TestFirmwareTemplateKeysFiltered`, covering Eletta `dXXX_rec_<P>_<K>`,
  PrimaDonna Soul `dXXX_<P>_rec_<K>` and the profile-less `dXXX_rec_<K>`
  layouts.

### Internals

- `cryptography` minimum bumped from `>=47.0.0` to `>=48.0.0`
  (Dependabot #26, automated tests green).

## [1.6.0-beta.14] — 2026-05-01

### Fixed

- **Profile sensor & select reported the wrong active profile**.
  `sensor.<dsn>_active_profile` previously preferred the monitor `profile`
  byte (60s polling) over the cloud `active_profile` property (10min
  refresh). The monitor byte can lag the user's selection because it
  tracks the *last brewed* profile, not the *currently selected* one;
  observed on Bastien's machine 2026-05-01 with `state="sk7n4k3d"` while
  `active_profile_id=3` ("fuck claud") was the actual selection on the
  machine UI. Both the sensor and `select.<dsn>_profile` now resolve the
  active profile id with priority `active_profile` (cloud, authoritative)
  → `profile` (monitor, fallback) → `1` (default), and the sensor
  exposes `monitor_profile_id` as a separate attribute so support reports
  can detect drift between the two sources.

- **UTF-16 decoder leaked adjacent struct fields into name strings**.
  `_decode_utf16` stripped null bytes globally instead of stopping at the
  first 2-byte-aligned NUL terminator, so fixed-width name slots
  containing `name1\\x00\\x00name2…` decoded as `name1name2…`. Bastien's
  bean profile names landed as `'PrédéfiniDémarrer\\x01Ā'`,
  `'Grains 1ఁ̀ā'`, `'Grains café 6Φλιτζά'` (Greek leaking into a French
  name). The decoder now truncates at the first aligned NUL pair before
  decoding and strips C0 control characters that leak from length-prefix
  bytes between fields. Locked by 7 regression tests in
  `TestDecodeUTF16FixedWidthBuffer`, including the exact pattern observed
  on the machine.

- **Brew button presses left automations blind during the cycle**.
  The 60s default poll interval missed every transient `machine_state`
  value (Pre-brewing, Brewing, Frothing milk, Rinsing, Steaming) because
  each lasts ~10-30s. The coordinator now exposes
  `request_fast_poll(duration_s, interval_s)` and `DeLonghiBrewButton`
  invokes it (`90s` window @ `5s` interval) immediately after sending the
  brew command, so watchers actually see the state flow. The fast-poll
  window expires automatically on the next cycle past its deadline,
  reverting to 60s. 5 tests in `TestFastPollWindow`.

- **`DeLonghiPowerSwitch._retry_task` polluted HA stage 2 boot**.
  `hass.async_create_task(self._retry_power_on())` registered the
  background sleeper in HA's "wait-for-this-to-finish" set, producing
  `Setup timed out for stage 2 waiting on …_retry_power_on()` warnings
  on every restart. Switched to `async_create_background_task` with the
  explicit name `delonghi_power_retry_<dsn>` so HA recognises it as a
  long-running background task and stops blocking boot on it.

## [1.6.0-beta.13] — 2026-05-01

### Fixed
- **Blocking alarms sensor — translation key rendered as `_3` suffix**.
  In beta.12 the new `DeLonghiBlockingAlarmsSensor` shipped with
  `_attr_translation_key = "blocking_alarms"` but no translation entry
  in any of the 16 language files, so HA fell back to the device name
  ("De'Longhi Eletta Explore") for friendly_name and produced
  `sensor.de_longhi_eletta_explore_3` because the device name was
  already taken. The 16 translation files now all carry an explicit
  `blocking_alarms.name` key, giving the sensor a stable
  `sensor.<dsn>_blocking_alarms` entity_id and a localized friendly
  name (en: Blocking Alarms, fr: Alarmes bloquantes, de: Blockierende
  Alarme, es: Alarmas bloqueantes, it: Allarmi bloccanti, nl: Blokkerende
  alarmen, pt: Alarmes de bloqueio, ru: Блокирующие тревоги, ja:
  ブロッキングアラーム, ko: 차단 알람, zh-Hans: 阻止性警报, pl: Alarmy
  blokujące, sv: Blockerande larm, da: Blokerende alarmer, nb:
  Blokkerende alarmer, uk: Блокувальні тривоги).

## [1.6.0-beta.12] — 2026-05-01

### Added
- **Blocking alarms surfaced — UX visibility for "machine stuck Turning On"**.
  When a blocking alarm is active (Water Tank Empty, Hydraulic Problem,
  Drip Tray Missing, Heater/Steamer Probe Failure, Infuser Motor Failure,
  Bean Hopper Absent, Grid Missing, Water Tank Missing, Grounds Container
  Full, Machine Service Required), the De'Longhi firmware accepts the
  POWER_ON sequence but never reaches Ready — the machine sits in
  `Turning On` indefinitely with no visible reason. Two complementary
  surfaces now make the cause obvious:
  - New `sensor.<dsn>_blocking_alarms` aggregator. State = number of
    blocking alarms currently active. Attributes:
    `blocking_alarms` (list of names), `blocking_bits` (raw alarm bits,
    useful for automations), `is_blocking_power_on` (bool),
    `all_active_alarms` (every active alarm, blocking or not).
  - `async_turn_on` now logs a WARNING with the explicit alarm names
    *and* schedules a `persistent_notification` in the HA UI so the
    user sees the issue in their notification tray instead of buried in
    logs. Power-on still proceeds (some alarm states clear themselves
    once the wake sequence runs); the announcement is purely
    informational and never aborts the command.
- `BLOCKING_ALARM_BITS: frozenset[int]` constant in `const.py`, derived
  once from the `blocking: True` flag now carried by each entry of
  `ALARMS`. Advisory alarms (Descale Needed, Cleaning Needed, Replace
  Water Filter, Coffee Beans Empty…) are *not* in the blocking set —
  the machine still operates with those active.
- 10 regression tests:
  `TestBlockingAlarmsSensor::test_zero_when_no_alarms`,
  `…test_zero_when_only_non_blocking_alarms`,
  `…test_counts_blocking_alarms`,
  `…test_handles_missing_alarms_key`,
  `…test_handles_alarm_without_name_falls_back_to_bit`,
  `TestBlockingAlarmsAnnouncement::test_no_blocking_alarms_is_silent`,
  `…test_only_advisory_alarms_is_silent`,
  `…test_water_tank_empty_warns_and_notifies`,
  `…test_multiple_blocking_alarms_listed`,
  `…test_announce_never_aborts_turn_on`. Suite total: 862 passed.

## [1.6.0-beta.11] — 2026-05-01

### Fixed
- **Power switch — turn_off cancels pending power-on retry, retry guards on
  user intent**. Sequence reproduced 2026-05-01 11:18→11:21 in HA log: a quick
  `turn_on` followed ~4s later by `turn_off` left the background `_retry_task`
  scheduled by `turn_on` armed. Three minutes after the original `turn_on`,
  the retry observed `machine_state=Off` (because `turn_off` had since
  succeeded), classified the original wake as failed, and re-sent
  `POWER_ON_CMD`. The machine, dutifully, switched back on against the user's
  explicit intent. Two complementary guards now prevent the regression:
  - `async_turn_off` cancels `_retry_task` if pending — the user just changed
    their mind, no point in racing them.
  - `_retry_power_on` aborts early when `_last_commanded_on is not True` — if
    a `turn_off` slipped through between the `turn_on` and the retry firing,
    the desired state is Off, not "wake again".
  Locked by `test_turn_off_cancels_pending_power_on_retry`,
  `test_turn_off_handles_no_pending_retry`,
  `test_retry_aborts_after_user_turned_off`,
  `test_retry_proceeds_when_last_commanded_still_on` (4 tests, all green).

## [1.6.0-beta.10] — 2026-04-29

### Fixed
- **PrimaDonna Soul "millcore" handshake — accept non-wallclock `time_1`**
  (#10). Reported by @dalodzik on `AC000W040821014`: the LAN handshake was
  rejected with `clock_skew` because the firmware ships `time_1` as a
  free-running monotonic counter (uptime nanoseconds, ≈1.4×10¹⁴ after a
  couple of days), not as UNIX seconds. The skew check now applies only
  when `time_1` looks like a wall-clock value (< 5×10⁹, year ≈2128); above
  that threshold the value is treated as a monotonic counter and the
  ratchet on `_last_handshake_time1` is the sole anti-replay control.
  Cremalink-style firmwares (Eletta Explore, PrimaDonna early FW) keep
  the original wall-clock guarantees. Locks the exact lodzen value with
  `test_handshake_accepts_primadonna_soul_uptime_time1`,
  `test_handshake_uptime_time1_still_enforces_anti_replay` and
  `test_handshake_still_rejects_wallclock_skew_replay`. Without this fix
  every PD Soul session fell back to the cloud, which never wakes the
  machine — power on/off and brewing all silently failed.
- **PrimaDonna Soul "millcore" model recognition** (#10). Decoder for
  `d270_serialnumber` already handled the binary envelope correctly
  (PR #19), but `_detect_contentstack_pattern` only scanned for
  `ECAM\d+` in the raw base64 string and didn't know about
  `DL-millcore`. The detector now consumes the decoded SKU through a
  new `SKU_TO_ECAM_PATTERN` map (currently `217055 → ECAM61075`), and
  the OEM map gained `DL-pd-soul`, `DL-pd-soul-better`, `DL-millcore`
  → `ECAM61075`. The user no longer sees `ContentStack: cannot
  determine ECAM model`; the downstream short-circuit on unindexed
  families still applies, so we don't spam HTTP for ECAM61.

### Security / CI
- **Release pipeline — verify tag matches manifest version** (#10).
  `release.yaml` now runs two cross-checks before publishing:
  the `manifest.json` at the tagged commit must declare the same
  version as the tag (`vX.Y.Z`), and the produced zip artefact must
  carry that exact manifest. A stale checkout, a forgotten bump, or a
  mistyped tag now fails the workflow loudly instead of shipping a
  release whose code doesn't match its name. Direct response to
  @dalodzik's "how do you test that your release commit is matching the
  expected release tag?".

### `delonghi_daedalus` 0.2.0 — security & diagnostics
This component is shipped from the same repository but is not bundled in the
HACS zip; install via manual clone until a separate HACS release pipeline is
ready.

#### Security
- **Drop password from entry data + add reauth flow** — the user's password
  is no longer persisted in `.storage/core.config_entries`. Runtime auth uses
  the long-lived Gigya session token (LAN AUTH frame uses the JWT). When the
  session token is revoked the coordinator now raises
  `ConfigEntryAuthFailed`, triggering HA's native reauth notification — the
  user is asked for the password only at that point.
- **LAN host validation** — `connect_lan` now rejects any host that isn't a
  numeric IP in a private / loopback / link-local range. The Daedalus
  firmware presents a self-signed cert, so the integration uses
  `verify_mode=CERT_NONE`. Without this guard, a public IP or hostname would
  let any on-path attacker MITM the WebSocket handshake and steal the JWT
  carried in the AUTH frame.

#### Added
- **Diagnostics endpoint** — HA-native "Download diagnostics" with secret
  scrubbing (jwt, session_token, session_secret, AuthToken, apiKey, host,
  serial_number, …). Safe to upload to a public GitHub issue.
- **Auto-probe Gigya pools on 403005** — already merged in PR #22, now
  locked by 7 unit tests covering preferred-pool / fallback / all-fail /
  short-circuit / ordering / log line.

#### Internal
- Sensors marked `EntityCategory.DIAGNOSTIC` to keep wiring-state out of
  default dashboards.
- Dropped dead `JWT_REFRESH_THRESHOLD_SECONDS` constant (refresh has always
  been reactive on auth failure, never proactive).
- 41/41 daedalus tests passing.

## [1.6.0-beta.9] — 2026-04-29

### Added
- **`delonghi_daedalus` companion integration** — separate component scaffolded for "My Coffee Lounge" / Eletta Ultra machines (package `com.delonghigroup.daedalus`). Independent config flow + auth path; the legacy `delonghi_coffee` component is unchanged for Coffee Link / Ayla machines. (#20)
- **Gigya pool selector** — daedalus config flow exposes the three documented Gigya pools (`EU`, `EU_US`, `CN`) so users on regional accounts can pick the right one explicitly instead of hard-failing on `403005`. Detailed auth/connect logging now surfaces the exact `errorCode` and `errorMessage` from Gigya in the HA log. (#21)
- **9 static beverages** — `drip_style`, `caffe_crema`, `red_eye`, `black_eye`, `espresso_intenso`, `ristretto_napoletano`, `babyccino`, `milchkaffee`, `koffie_verkeerd` — registered with localized labels in every shipped locale.

### Fixed
- **Custom slot wire-key mismatch** — custom drinks were registered under `custom_bev_1..6` while the wire format reports them as `custom_1..6`. The integration now uses the wire keys (matching `coordinator.custom_recipe_names`), eliminating the recurring `Unknown beverage keys — buttons created with default name/icon: ['custom_1', ...]` warning. The brew button also now falls back to the user's custom name when set, so e.g. a slot renamed "Booster" wins over the localized "Custom Drink 1" label. Locks the contract with `TestCustomSlotsUseWireKey` so it can't regress.
- **Binary-encoded device serial** — `api.py` now decodes base64-encoded binary serials reported by PrimaDonna Soul and other modern firmwares, instead of treating them as opaque strings. (#19)
- **Coordinator unexpected-exception path** — log the exception before re-raising as `UpdateFailed` so the underlying error is visible in HA logs instead of being silently wrapped.

### Internals
- **Timing constants extracted** — `MONITOR_STALE_TIMEOUT`, `POWER_WAKE_DELAY`, `POWER_RETRY_DELAY`, `POWER_STALE_THRESHOLD` moved to `const.py` (legacy `_WAKE_DELAY` etc. kept as aliases so private callers still resolve).
- **BLE001 rationale comments** added on the broad `except` in `config_flow`.
- **812 tests** passing (was 769 at beta.8), ruff clean.

## [1.6.0-beta.8] — 2026-04-17

### Security
- **LAN server hardening** — bind defaults to `127.0.0.1` instead of `0.0.0.0`, peer allowlist enforced on every handler (handshake, command poll, property push, status), clock-skew guard rejects handshakes more than 30 min from wall-clock, anti-replay on `time_1` blocks attackers from rolling the device back with a captured handshake. Coordinator wires the actual LAN IP into `allowed_peers` automatically.
- **Canonicalize base64 random values** — `_handle_handshake` and `run_lan_diagnostic` now strip trailing `=` padding on received `random_1` / `random_2`, so a non-conforming peer can't make the two sides derive different session material.

### Added
- **Keepalive backoff escalation** — coordinator escalates WARNING → ERROR after 3 consecutive ping failures and fires ERROR every 5 subsequent attempts, with an INFO line on recovery. Stops quiet degradation going unnoticed in logs.
- **LAN diagnostic hard timeout** — `run_lan_diagnostic` now wraps the whole pipeline in a 30 s `asyncio.timeout`, reports `teardown_ok` / `teardown_error` in the result details, and uses `asyncio.shield` for server teardown so cancellation doesn't leak a runner.

### Fixed
- **Button availability (`brew`, `sync_recipes`)** — now also gate on machine state `Off` / `Sleep`. Pressing a button while the machine was asleep used to silently drop the command.
- **Service registration** — HA services are now registered once per HA instance (not once per entry) via a `hass.services.has_service(...)` guard. Handlers resolve `(api, coord, dsn)` lazily from `call.data["config_entry_id"]`, so multiple coffee machines no longer fight over the same handler closure.
- **Diagnostic button except** — narrowed from bare `Exception` to `(DeLonghiApiError, DeLonghiAuthError, requests.RequestException, TimeoutError, ValueError, KeyError)`; programmer errors (AttributeError, TypeError) now crash loudly instead of being silently wrapped as "cloud fetch failed".

### Internals
- **46 new tests** — `tests/test_config_flow.py` (12 cases: user step happy/error branches, reauth, options flow), `tests/test_diagnostics.py` (7 cases: every `REDACT_KEYS` entry scrubbed, triage fields surface, missing entry tolerated), `tests/test_button.py` (+19 cases covering availability + dispatch + error mapping on all four button types), `tests/test_select.py` (11 cases covering options / current_option / select_option), plus canonicalization regression in `tests/test_lan.py`. Suite total 466, ruff clean.

## [1.6.0-beta.7] — 2026-04-17

### Fixed
- **LAN concurrency** — `_handle_handshake` used to swap `self._session` outside the lock while a concurrent `_handle_command_poll` could already have read the old pointer and bumped `self._seq` against the new counter, producing desynced IV chains the device silently rejected. Both paths now take the same lock when touching session/seq; derive/encrypt stay off-lock. Regression tests hammer handlers with `asyncio.gather` to keep the invariant enforced.

### Notes
- `v1.6.0-beta.6` was cut from `master` before the fix landed — prerelease channel users should skip straight to beta.7.

## [1.6.0-beta.6] — 2026-04-14

### Added
- **Diagnostics download** — HA-native "Download diagnostics" for faster bug triage (secrets scrubbed).

### Fixed
- **Manifest** — declare `cryptography>=41.0.0` requirement now that LAN crypto is wired.
- **Services** — `write_bean_profile` profile index bounds aligned with API (1-5, not 1-4).
- **API token refresh** — thread-safe; races could previously submit a stale token once per reload.
- **Button listener leak** — coordinator subscription is now cleaned up across entry reload.
- **Release workflow** — SemVer-strict prerelease detection (`vX.Y.Z-<tag>`), stops promoting beta tags to stable.

### Changed
- **FR translations** — missing options entries added.
- **Coverage** — stop hiding 8 files from coverage; expose the real number.

## [1.6.0-beta.5] — 2026-04-11

### Added
- **LAN Phase 2** — cremalink-compatible crypto (AES-256-CBC + HMAC-SHA256), IV chaining, embedded `aiohttp` server on port 10280, endpoints `/local_reg.json`, `/local_lan/key_exchange.json`, `/local_lan/commands.json`, `/local_lan/property/datapoint.json`. Observe-mode wiring into coordinator behind an opt-in flag.
- **LAN diagnostic button** — on-demand handshake + round-trip check with dedicated debug logger namespace.

### Fixed
- **Counters** — PrimaDonna Soul integer layout `d733..d748` now decoded correctly (fixes #3).
- **LAN config keys** — honor `lan_enabled` / `lanip_key` / `lan_ip` from options as documented.

## [1.5.x] and earlier

See the [GitHub releases](https://github.com/sk7n4k3d/delonghi-ha/releases) page.

[Unreleased]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.7.1...HEAD
[1.7.1]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0...v1.7.0
[1.6.0-beta.9]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.8...v1.6.0-beta.9
[1.6.0-beta.8]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.7...v1.6.0-beta.8
[1.6.0-beta.7]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.6...v1.6.0-beta.7
[1.6.0-beta.6]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.5...v1.6.0-beta.6
[1.6.0-beta.5]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.5.10...v1.6.0-beta.5
