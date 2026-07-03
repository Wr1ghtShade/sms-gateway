# Changelog — SMS Gateway

---

## [1.00.36] — 2026-07-03
### Sécurité & robustesse
- **`save_config()`** : `fsync()` ajouté avant le `os.replace()` — les données sont garanties sur disque avant le rename, plus de risque de corruption en cas de coupure d'alimentation brutale du Pi. `fchmod(0o660)` fixe aussi les permissions du fichier temporaire indépendamment de l'umask du process.
- **`/send_bulk/stream`** : ajout de `@limiter.limit('20 per minute')` — évite qu'un flood de connexions SSE simultanées épuise les threads du serveur de dev Flask.

---

## [1.00.35] — 2026-07-03
### Performance
- **`adapters/netgear.py`** : session HTTP + login au modem désormais persistants sur un event loop dédié (thread daemon), au lieu d'ouvrir une nouvelle session et de se reconnecter à chaque appel — impactait notamment le polling `/router/status` toutes les 5s. Reconnexion automatique et sans retry sur erreur `eternalegypt.Error` (session expirée côté routeur), pour éviter tout risque de double-envoi de SMS.
- **`adapters/base.py`** : ajout de `close()` (no-op par défaut) pour libérer proprement les ressources persistantes d'un adaptateur.
- **`gateway-sms-webui.py`** : `reload_adapter()` et `/config/test` ferment désormais l'ancien/l'adaptateur temporaire via `close()` — évite une fuite de thread/event loop à chaque reconfiguration ou test de connexion Netgear.

---

## [1.00.34] — 2026-06-19
### Fix
- **`/send` & `/send_bulk`** : ajout de `normalize_number()` — les numéros avec espaces (`06 XX XX XX XX`), tirets (`06-XX-XX-XX-XX`) ou points (`06.XX.XX.XX.XX`) sont désormais nettoyés avant validation **et** avant envoi au routeur. Formats supportés : `06`, `07`, `+336`, `+337` avec ou sans séparateurs.

---

## [1.00.33] — 2026-06-09
### Fix
- **`/send`** : décorateur `@csrf_required` retiré — les appels POST JSON depuis Home Assistant (et autres intégrations LAN) n'incluent pas de header `X-CSRF-Token`, ce qui causait un 403. Risque accepté pour un déploiement LAN-only.

---

## [1.00.32] — 2026-06-09
### Fix
- **`/send`** : méthode GET rétablie — les intégrations LAN (Home Assistant, NUT, scripts shell) n'utilisent pas de CSRF token et appelaient l'endpoint en GET. Risque accepté pour un déploiement LAN-only (note ajoutée dans le README).

---

## [1.00.31] — 2026-06-09
### Sécurité & robustesse (code review)
- **`save_config`** atomique : écriture dans `.tmp` + `os.replace()` — plus de corruption JSON en cas de crash/coupure
- **`/send`** : méthode GET retirée (numéro + message ne transitent plus en clair dans l'URL et les logs)
- **Security headers** : `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` ajoutés sur toutes les réponses Flask
- **ZTE adapter** : session HTTP locale par méthode (thread-safe) — `self._session` supprimé ; `check_health` ferme proprement la connexion
- **Background threads** : `perform_bulk_delete` et `perform_bulk_send` utilisent `_get_adapter_for_thread()` au lieu de `current_adapter()` — plus de `flask.abort()` depuis un thread sans contexte d'application
- **`BulkDeleteState.error`** : type corrigé `str = None` → `Optional[str] = None`
- **SSE stream** : deadline portée de 2s à 5s + les logs d'une opération à completion rapide sont correctement renvoyés
- **JS** : dead code supprimé (groupes mode `simple` jamais implémenté dans `refreshGroupSelects`)
- **`previewFile`** : limite 5 MB ajoutée — les fichiers trop lourds ne bloquent plus le thread JS
- **README.md** : ajouté en anglais avec screenshots, routes API, intégration externe
- **`fix-perms.sh`** et dossier `docs/` ajoutés au script de sync

---

## [1.00.30] — 2026-06-06
### Sécurité & robustesse (audit code review)
- **`load_config`** robustifié : JSON corrompu ou racine non-dict → fallback config vide + log d'erreur (le service ne crashe plus au démarrage)
- **`reload_adapter`** : swap atomique de `_config` / `_adapter` sous `threading.Lock` — fini les états torn (nouvelle config, ancien adapter) en cas de requête concurrente
- **`current_adapter()`** : snapshot sous lock pour ne jamais retourner un adapter en cours de remplacement
- **`/router/status`** : cache thread-safe (lock séparé, appel routeur hors du lock pour ne pas sérialiser tous les callers)
- **Validation marque** dans `POST /config` et `/config/test` : `brand in ADAPTERS` vérifié upfront → erreur 400 propre au lieu d'une exception générique
- **TPLink** : distinction erreur réseau vs erreur d'auth — un routeur injoignable ne déclenche plus le fallback GCM (qui échouerait de toute façon)
- **GL.iNet** : `/tmp/glinet_cache` durci à `chmod 700` à chaque instanciation
- Suppression de `.env` et `.env.example` (morts depuis la bascule vers `router_config.json`)
- `fix-perms.sh` : ligne `chmod 640 .env` retirée

---

## [1.00.29] — 2026-06-06
### Nouveautés
- Adapteur **ZTE MC/MF** (MC801a, MC889, MF286, MF289…) via API goform HTTP native
- Auth SHA256 double-hash + cookie stok, token AD pour les opérations d'écriture
- Encodage/décodage SMS UCS-2 hex (UTF-16-BE)
- Logo SVG ZTE inline (carré bleu "Z")

---

## [1.00.28] — 2026-06-06
### Nouveautés
- Adapteur **TP-Link MR** (MR6400, MR600, MR500, MR200…) via `tplinkrouterc6u`
- Auto-détection firmware : RSA+AES (`TPLinkMRClient`) vs AES-GCM (`TPLinkMRClientGCM`)
- Suppression par batch en session unique
- Logo SVG TP-Link inline (carré rouge "TP/LINK")

---

## [1.00.27] — 2026-06-06
### Performance
- `prefetchConfig()` : configuration chargée en arrière-plan au démarrage de la page
- Affichage instantané de l'onglet ⚙️ Config (zéro délai réseau au clic)
- Cache `_configCache` invalidé uniquement après une sauvegarde réussie

---

## [1.00.26] — 2026-06-06
### UX
- Logos SVG inline pour chaque marque : fleur rouge Huawei, carré violet Netgear, hexagone vert GL.iNet
- Grille 3 colonnes, cartes compactes (une ligne par marque)
- Label "Config" ajouté à côté de la roue crantée

---

## [1.00.25] — 2026-06-06
### Nouveautés
- Adapteur **GL.iNet LTE/5G** (X3000, XE3000, X750, E750…) via `python-glinet`
- JSON-RPC 2.0 avec challenge-response auth
- Bus modem auto-découvert au premier appel, mis en cache ensuite
- `keep_alive=False` pour compatibilité Flask
- Cache credentials dans `/tmp/glinet_cache` (compatible www-data)

---

## [1.00.24] — 2026-06-06
### Sécurité
- **SSRF** : `validate_router_ip()` — seules les IPv4 privées acceptées sur `/config` et `/config/test`
- **Rate limiting** Flask-Limiter : 10 req/min sur `/config` et `/config/test`
- **CSRF token** sur toutes les routes POST sensibles
- Mot de passe masqué dans les logs et dans la réponse de `GET /config`

---

## [1.00.23] — 2026-06-06
### Architecture
- Suppression du fallback `.env` — `router_config.json` devient la seule source de vérité
- L'UI ou l'édition manuelle du fichier JSON remplace entièrement les variables d'environnement
- `router_config.json` non versionné (`.gitignore`), permissions 660 (`gauthi3r:www-data`)

---

## [1.00.22] — 2026-06-06
### Nouveautés
- Onglet **⚙️ Config** dans l'interface web
- Grille de sélection de marque, champs IP / utilisateur / mot de passe
- Bouton **Tester** : vérifie la connexion sans sauvegarder
- Bouton **Sauvegarder** : applique la config immédiatement, sans redémarrage du service
- Nouvelles routes API : `GET /config`, `POST /config`, `POST /config/test`, `GET /capabilities`

---

## [1.00.21] — 2026-06-06
### Nouveautés
- Adapteur **Netgear LTE** (LB1120, LB2120, MR1100…) via `eternalegypt`
- Bridge async/await → sync via `asyncio.run()` pour compatibilité Flask
- Pagination en mémoire, tri par ID décroissant
- Inbox uniquement (`supports_outbox = False`)

---

## [1.00.20] — 2026-06-06
### UX
- Onglet Outbox désactivé visuellement (opacité 0.4) si le routeur ne supporte pas l'outbox
- Message "non supporté" affiché dans l'outbox en lieu et place d'une erreur

---

## [1.00.19] — 2026-06-06
### Architecture
- Refacto complet en architecture **multi-adapteurs**
- `adapters/base.py` : classe abstraite `RouterAdapter` + `NotSupportedError`
- `adapters/huawei.py` : `HuaweiAdapter` (Huawei LTE, inbox + outbox)
- `adapters/__init__.py` : factory `get_adapter(config)` + registre `ADAPTER_META`
- Suppression de la logique Huawei directement dans `gateway-sms-webui.py`

---

## [1.00.18] — 2026-06-04
### UX
- Suppression du `padding-right` sur le `h1` qui décentrait le titre "SMS Hub"

---

## [1.00.17] — 2026-06-04
### UX
- Boutons de thème remis en `position: absolute` en haut à droite (24px, plus petits)
- Padding réservé sur le `h1` pour éviter le chevauchement sur mobile

---

## [1.00.16] — 2026-06-04
### UX
- Suppression du `padding-right` sur le `h1` qui décentrait le titre "SMS Hub"

---

## [1.00.15] — 2026-06-04
### UX
- Boutons de thème remis en `position: absolute` en haut à droite (24px, plus petits)
- Padding réservé sur le `h1` pour éviter le chevauchement sur mobile

---

## [1.00.14] — 2026-06-04
### UX
- Boutons de thème réduits (26px) et repositionnés en ligne centrée pour corriger le chevauchement mobile

---

## [1.00.13] — 2026-06-04
### Nouveautés
- Widget statut routeur : signal (barres), opérateur, type réseau (4G LTE, etc.), rafraîchi toutes les 30s
- Compteur de caractères SMS sous chaque textarea : détection GSM-7 vs Unicode, nombre de SMS calculé
- Endpoint `GET /health` : répond 200 si le routeur est joignable, 503 sinon
- Endpoint `GET /router/status` : retourne signal, opérateur, type réseau, solde
- Script `fix-perms.sh` : remet les bonnes permissions après édition (gauthi3r:www-data)

---

## [1.00.12] — 2026-06-04
### Corrections de bugs
- Validation des numéros ajoutée dans `perform_bulk_send`
- Validation upfront dans `/send_bulk` : la route rejette toute la liste si un numéro est invalide
- Projet déplacé de `/root/sms-gateway` vers `/var/www/sms-gateway`
- Service systemd migré de `root:root` vers `www-data:www-data`
- Permissions : `gauthi3r` propriétaire (écriture), `www-data` groupe (lecture/exécution)

---

## [1.00.11] — 2026-06-04
### UX
- Suppression de la confirmation d'envoi (dialog inutile avant chaque envoi)

---

## [1.00.10] — 2026-06-04
### Sécurité
- Credentials (HUAWEI_USER, HUAWEI_PASS, HUAWEI_IP) sortis du code source
- Chargement via `python-dotenv` depuis un fichier `.env`
- Ajout d'un `.env.example` comme template versionnable
- Démarrage bloqué avec message clair si HUAWEI_PASS manquant

---

## [1.00.09] — 2026-06-04
### Refacto
- HTML (~400 lignes) extrait du fichier Python vers `templates/index.html`
- `render_template_string(HTML_PAGE)` remplacé par `render_template('index.html')`
- `sms-ups-alert-flask.py` supprimé (fonctionnellement remplacé par le projet principal)

---

## [1.00.08] — 2026-06-04
### UX
- Ajout d'un bouton ⏹ Stop visible pendant un envoi groupé
- Appel à `POST /send_bulk/stop` pour interrompre le thread d'envoi entre deux SMS
- Message de log affiché en cas d'annulation par l'utilisateur

---

## [1.00.07] — 2026-06-04
### UX
- Zone de progression (barre + logs) désormais persistante lors des changements d'onglet pendant un envoi
- Variable `isSending` côté JS pour bloquer le masquage de la zone de progression

---

## [1.00.06] — 2026-06-04
### Performance
- Envoi groupé migré en arrière-plan serveur via `POST /send_bulk`
- Réduction de N requêtes HTTP à 1 seule depuis le navigateur
- Ajout de `GET /send_bulk/status` pour le polling du statut
- Ajout de `POST /send_bulk/stop` pour l'annulation
- Page fluide pendant tout l'envoi (plus de boucle bloquante JS)

---

## [1.00.05] — 2026-06-04
### Corrections de bugs
- `if not idx` → `if idx is None` (index SMS = 0 ne déclenchait plus une erreur à tort)
- Suppression de la réinitialisation d'état redondante dans `perform_bulk_delete`
- Limite hardcodée à 15 itérations remplacée par `while True`

---

## [1.00.04] — 2026-06-04
### UX
- Suppression de l'alerte de succès après une suppression globale de l'outbox (doublon pénible)

---

## [1.00.03] — 2026-06-04
### Refacto
- Extraction de `parse_sms_list()` — logique de parsing SMS dédupliquée (3 occurrences → 1 fonction)
- `ROUTER_URL` définie comme constante globale
- `deleteSms(index, onSuccess)` — fusion de `deleteSms` et `deleteSentSms` en une seule fonction JS avec callback

---

## [1.00.02] — 2026-06-04
### Sécurité
- Validation des numéros de téléphone côté serveur
- Formats acceptés : `06XXXXXXXX`, `07XXXXXXXX`, `+336XXXXXXXX`, `+337XXXXXXXX`
- Espaces et tirets nettoyés avant validation
- Réponse `400` avec message explicite si numéro invalide

---

## [1.00.01] — Version initiale
- Interface web Flask pour l'envoi de SMS via routeur Huawei LTE
- Modes d'envoi : Simple, File (.txt), Expert (numéro;message)
- Lecture Inbox / Outbox
- Suppression individuelle et suppression globale de l'outbox (arrière-plan)
- 4 thèmes UI : Clair, Sombre, Cyber, Dracula
- Sanitization du mot de passe dans les messages d'erreur
