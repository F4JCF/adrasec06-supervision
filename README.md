# Supervision MeshCore ADRASEC 06

Programme Windows de supervision du réseau LoRa MeshCore de l'ADRASEC 06 : carte des nœuds, états, firmware, liaisons SNR, actions en attente, journal d'exploitation, et **lecture automatique des répéteurs** via un nœud MeshCore relié en USB ou en Bluetooth.

## Ce qu'il faut

- Windows 10 ou 11 (le moteur d'affichage Microsoft Edge WebView2 est déjà présent sur ces versions).
- Un nœud MeshCore (Heltec V3, T096, T1000-E, Wio Tracker L1…) flashé avec le Web Flasher MeshCore :
  - en **Companion USB (serial)** pour une liaison par câble ;
  - ou en **Companion Bluetooth** pour une liaison sans fil (PC avec Bluetooth 4.0 ou plus).
- Le mot de passe **admin** de chaque répéteur ADRASEC que vous voulez interroger complètement.

## Utilisation

1. Lancez `Supervision-ADRASEC06.exe`.
2. Dans **Liaison avec le nœud** :
   - **USB** : choisissez le port COM du nœud (Actualiser si besoin), puis **Connecter** ;
   - **Bluetooth** : cliquez **Rechercher**, choisissez le nœud (ou laissez « Premier nœud MeshCore trouvé »), saisissez son code **PIN** (affiché sur l'écran du nœud ou défini dans sa configuration, souvent 123456), puis **Connecter**.
3. Ouvrez la fiche de chaque répéteur ADRASEC, cliquez **Modifier** et saisissez son **mot de passe admin**. Le nom du nœud doit être exactement celui qu'il annonce sur le réseau.
4. Le programme interroge le réseau à l'intervalle choisi (15 min par défaut) ou tout de suite avec **Interroger maintenant**.

### Bluetooth : à savoir

- Un nœud companion Bluetooth n'accepte qu'**une seule connexion** : fermez l'application MeshCore du téléphone (ou déconnectez-la) avant de connecter le PC.
- Si la connexion échoue malgré le bon PIN, appairez d'abord le nœud dans **Paramètres Windows → Bluetooth et appareils → Ajouter un appareil**, puis reconnectez depuis le logiciel.
- La portée Bluetooth est de quelques mètres : gardez le nœud près du PC. La liaison se reconnecte seule en cas de coupure brève.
- Le logiciel retient le dernier mode, le dernier nœud et le PIN utilisés.

### Ce qui est lu automatiquement

Pour chaque répéteur ADRASEC (hors « Prévu ») présent dans les contacts du nœud companion :

| Donnée | Sans mot de passe | Avec mot de passe admin |
|---|---|---|
| Bruit, erreurs RX, batterie, uptime, paquets | si le répéteur accepte la connexion invité | oui |
| Voisins avec SNR (liaisons sur la carte) | non | oui |
| Version firmware (`ver`) et puissance (`get tx`) | non | oui |

- **État** : un répéteur qui ne répond pas passe en *Dégradé* au 1er échec et *Hors ligne* au 3e. Il repasse *En ligne* dès qu'il répond. Chaque changement est noté dans le journal (auteur `AUTO`). Les nœuds *En test* ne changent pas d'état automatiquement.
- **Découverte** : avec « Ajouter les répéteurs entendus », chaque répéteur présent dans les contacts du nœud companion est ajouté comme voisin (marqué `auto`), avec sa position GPS s'il l'annonce.
- **Historique** : bruit, erreurs RX et batterie sont conservés 90 jours. La fiche affiche les 7 derniers jours.

### Nouveautés de la version 1.1

- **Messagerie** (onglet Messagerie) : lire et envoyer sur les canaux MeshCore du nœud companion (Public, ADRASEC…) et en message direct. 150 octets par message, Entrée pour envoyer.
- **Carte réelle** : fond OpenStreetMap France, OpenStreetMap ou relief OpenTopoMap (au choix dans Réglages ; « Schéma hors ligne » sans Internet).
- **Chemin** : la fiche d'un répéteur montre par quels relais passe la liaison depuis le nœud companion ; le trajet s'affiche en pointillés sur la carte.
- **Commandes à distance** (fiche du répéteur, mot de passe admin requis) : puissance TX, advert, économie d'énergie, redémarrage. Chaque commande est confirmée puis notée au journal.
- **Alertes** : notification Windows et bip quand un répéteur passe hors ligne, qu'une batterie passe sous le seuil, ou qu'un message direct arrive.
- **Main courante** : démarrer un exercice, une intervention ou une veille ; toutes les entrées du journal y sont rattachées ; export PDF avec les messages radio échangés.
- **Rapports** : état du réseau en PDF ou Excel.
- **Arrière-plan** : la croix réduit près de l'horloge, démarrage automatique avec Windows (Réglages) avec reconnexion au dernier nœud.

### Nouveautés de la version 1.2

- **Équipes sur la carte** : les mobiles et companions qui partagent leur position GPS apparaissent (losange violet) avec leur trace des 6 dernières heures.
- **Messages types** : liste de modèles au-dessus de la zone de saisie ; [heure] et [indicatif] remplis automatiquement ; modèles modifiables dans Réglages.
- **Salle de crise** : bouton en haut à droite, plein écran avec carte, état des répéteurs, alertes et derniers messages (Échap pour sortir).
- **Télémétrie** : tension, température, charge… affichées dans la fiche des répéteurs qui les transmettent.
- **Statistiques** : disponibilité sur 7 et 30 jours et dernière coupure de chaque répéteur ; la disponibilité figure aussi dans le rapport PDF/Excel.
- **Test de couverture** : depuis un point de mesure, SNR aller/retour vers les répéteurs voisins directs et nombre de relais pour les autres ; mesures visibles sur la carte.
- **Maintenance** : dates d'installation, de changement de batterie, de visite, accès au site ; rappel 7 jours avant la prochaine visite.
- **Sauvegarde automatique** quotidienne dans le dossier de votre choix (14 copies gardées).
- **Mise à jour automatique** : le logiciel vérifie les nouvelles versions publiées sur GitHub (dépôt public) et s'installe en un clic.

### Nouveautés de la version 1.3

- **Mise à jour du firmware des répéteurs** (fiche du répéteur, mot de passe admin requis) : « Préparer la mise à jour » envoie `start ota` par le réseau ; le répéteur ouvre le Wi-Fi **MeshCore-OTA**. Sur place, à portée Wi-Fi, ouvrir `http://192.168.4.1/update` et envoyer le fichier `.bin` Repeater (pas le « merged »). « Vérifier la version » confirme la mise à jour et la note au journal ; « Remettre l'heure » envoie `clock sync`. Cartes ESP32 uniquement (Heltec V3, T096, Xiao…) ; les cartes nRF52 se mettent à jour en Bluetooth sur place.

### Nouveautés de la version 1.4 — main courante

- **Ouverture** : opération, type, lieu, autorité demandeuse, cadre / mission.
- **Saisie rapide** dans l'onglet : heure (vide = maintenant), de, à, nature (information, demande, ordre, compte rendu, événement), message ; Ctrl + Entrée pour inscrire. Entrées numérotées.
- **Suite et clôture** de chaque entrée ; ordres et demandes sans suite signalés « en attente » (filtre dédié).
- **Moyens engagés** : indicatif, nom, fonction, équipe, matériel, secteur ; boutons Arrivé / Parti inscrits au registre ; position GPS affichée si l'équipe la partage.
- **Clôture** avec bilan ; **PDF officiel** : en-tête ADRASEC 06, cadre, moyens, registre numéroté, annexe des messages radio, bilan, signatures, page x / y.

### Nouveautés de la version 1.5 — APRS

- **Positions APRS par Internet (APRS-IS)**, en réception seule (aucune émission, code -1) : Réglages → APRS, liste d'indicatifs suivis (sans SSID = toutes les variantes) et/ou rayon autour de Mougins.
- Triangle bleu sur la carte (et en salle de crise) avec indicatif, âge de la position, vitesse, cap, altitude, commentaire et trace sur 6 h ; case « APRS » pour afficher ou masquer ; lien vers aprs.fi.
- Moyens engagés : « position APRS il y a … » quand l'indicatif est reçu.
- Un opérateur repéré en APRS qui écrit sur #ADRASEC 06 passe aussi en orange fluo.
- Il faut Internet ; seules les stations reçues par une iGate sont visibles.

### Nouveautés de la version 1.6

- **Collecte** : toutes les 5 min, 1 h, 3 h, 6 h, 12 h, 24 h, ou **à heures fixes** (ex. « 08:00, 20:00 »).
- **Encart « Répéteurs ADRASEC »** en haut de l'onglet Réseau : état, batterie, bruit, disponibilité, firmware, dernière lecture ; FR06-MOUGins-F4JCF y figure et est interrogé.
- **Redémarrer** : la reprise est vérifiée (temps de fonctionnement) et notée au journal.
- **Éteindre à distance** (`poweroff`) avec double confirmation : le répéteur ne peut être rallumé que sur place ; l'extinction est vérifiée, la surveillance suspendue (pas d'alertes) jusqu'au bouton « Rallumé : reprendre la surveillance ».

### Nouveautés de la version 1.7

- **Liste des nœuds** : case à cocher par ligne et « tout cocher » (liste affichée) ; barre d'actions : **Supprimer la sélection** (état Supprimé, réversible avec « Rétablir » dans le filtre Supprimés) ou **Effacer définitivement** (avec confirmation). Un voisin effacé n'est plus ajouté par la découverte automatique.
- **Messagerie** : croix au survol d'un message pour le supprimer, bouton **Vider la conversation** (avec confirmation). La suppression ne concerne que l'historique du PC.
- 1.7.1 : un opérateur qui a écrit sur #ADRASEC 06 (6 h) est signalé par un **triangle orange** sur la carte.

### Nouveautés de la version 1.8

- Titre « Supervision Réseau Meshcore Adrasec 06 » avec le logo ; la fenêtre s'ouvre en plein écran et l'affichage (largeur, hauteur de la carte) s'adapte à la taille de l'écran, y compris sur les portables 1366 × 768.

### Nouveautés de la version 1.9

- **Détail d'un message** : un clic sur un message (messagerie ou salle de crise) ouvre son parcours : émetteur, relais traversés dans l'ordre, SNR et RSSI à la réception, et une carte du trajet. Si le message est arrivé par plusieurs chemins, chacun est affiché (trait plein puis pointillés). Pour vos propres messages, les échos des relais qui les ont répétés sont listés.
- Les relais sont identifiés par le début de leur clé ; quand plusieurs répéteurs partagent le même identifiant, le plus proche du trajet est retenu (« probable »).
- Messages directs : chiffrés de bout en bout, seul le nombre de sauts est connu ; la route connue vers le contact est affichée.
- Le parcours n'est disponible que pour les messages reçus à partir de cette version.

### Nouveautés de la version 1.10

- **Moyens engagés** : menus déroulants pour l'indicatif, la fonction, l'équipe, le matériel et le lieu. Listes de base + valeurs déjà utilisées dans les mains courantes précédentes + « Autre… » pour saisir librement. Choisir un opérateur déjà connu remplit automatiquement son nom, sa fonction, son équipe, son matériel et son lieu habituels ; les indicatifs suivis en APRS sont proposés.

### Nouveautés de la version 1.11

- Fiche d'un nœud : date et heure de lecture de la télémétrie et du chemin.
- **Réinitialiser le chemin** (toutes les fiches, nœud companion connecté, sans mot de passe) : le nœud companion oublie la route mémorisée vers ce nœud ; pour un répéteur, il l'interroge aussitôt pour réapprendre un chemin. Avant/après noté au journal.

### Nouveautés de la version 1.12

- **Remise à l'heure automatique** des répéteurs décalés de plus de 2 min (mot de passe admin requis), notée au journal ; le nœud companion prend l'heure du PC à la connexion.
- **Alerte précoce** : bruit en hausse, erreurs RX en forte hausse, batterie en baisse continue, liaison voisine affaiblie → notification, journal, encadré dans la fiche et la vignette.
- **Autonomie** : courbe du minimum de batterie de chaque jour (30 j) et estimation des jours restants avant 3,30 V.
- **Maillons faibles** (onglet Statistiques) et infobulle des liaisons dans les deux sens sur la carte.
- **Tester la liaison (aller-retour)** depuis la fiche : SNR mesuré à chaque saut.
- **Appel des stations** sur #ADRASEC 06 : réponses cochées automatiquement, clôture inscrite à la main courante.
- **Accusé de réception** des messages directs (✓ reçu / ✗ non confirmé, 3 essais) et bouton Renvoyer.
- **Alertes par e-mail** (SMTP, ex. Infomaniak) regroupées par minute, e-mail d'essai.
- **Tableau de bord sur tablette/téléphone** (lecture seule, réseau local, code d'accès facultatif).
- **Rapport hebdomadaire** PDF automatique (jour et heure au choix), envoi possible par e-mail.

### Nouveautés de la version 1.13

- **Mains courantes** : bouton **Archiver** (PDF + données .json enregistrés dans le dossier d'archives choisi, puis retirée de la liste ; « Afficher les archivées » / « Désarchiver ») et bouton **Supprimer** (avec confirmation ; seulement une main courante clôturée).
- **Analyser mon nœud** (panneau Liaison) : identité, appareil et firmware, paramètres radio comparés au réseau ADRASEC 06, puissance, batterie et mémoire, horloge (remise à l'heure si décalée), statistiques radio et trafic, partage de position et télémétrie, contacts et canaux, avec un diagnostic des points à vérifier.
- Vignettes ADRASEC : **pourcentage de batterie** à côté de la tension.
- **Horloge** HH:MM:SS et date en haut à droite.

### Nouveautés de la version 1.14

- Vignettes ADRASEC : **température** transmise en télémétrie (orange à partir de 45 °C, rouge à 60 °C).
- **Pourcentage de batterie** calculé comme l'écran des nœuds MeshCore (3,00 V = 0 %, 4,20 V = 100 %) ; **correction de tension** du nœud companion réglable (Réglages) ; l'analyse du nœud compare la mesure batterie et la télémétrie.
- **Appel des stations…** : choix des stations appelées (moyens engagés, stations ayant écrit sur le canal, contacts, ajout manuel), indicatifs cités dans le message si la place le permet ; le suivi pointe ✓ / ⏳ chaque station appelée et liste les autres réponses ; la clôture inscrit à la main courante les stations ayant répondu et celles sans réponse.

### Nouveautés de la version 1.15

- **Définir le chemin…** dans le détail d'un message : choisir soi-même les relais (dans l'ordre) par lesquels passent les messages directs vers ce contact, ou le mode direct, ou l'inondation ; le chemin est enregistré dans le nœud companion et noté au journal.
- Le bouton **Renvoyer** n'ouvre plus le détail du message ; **coche verte** et nombre de répéteurs ayant relayé nos messages de canal ; bouton **Répondre** dans le détail.

### Nouveautés de la version 1.16

- L'onglet « Main courante & rapports » s'appelle désormais **ADRASEC 06**.
- **Test mensuel de résilience** (sous « Rapports d'état du réseau ») : diffuse sur #ADRASEC 06 « [EXERCICE ADRASEC 06 - TEST MENSUEL DE RESILIENCE - NE PAS REPONDRE - ACK AUTO] », interroge les répéteurs et écoute 10 minutes (répéteurs qui relaient, accusés automatiques, état des répéteurs ADRASEC) ; suivi en direct, compte rendu PDF rangé avec les rapports (et envoyé par e-mail si activé), historique ; lancement automatique possible chaque mois (jour et heure au choix).

### Nouveautés de la version 1.17

- **Console distante** (bouton dans chaque fiche nœud, mot de passe admin requis) : envoyer n'importe quelle commande console MeshCore au nœud et lire sa réponse ; boutons rapides (ver, clock, get radio, get tx, neighbors…), historique avec ↑ ↓, confirmation pour les commandes sensibles (set, reboot, password, erase…), chaque commande notée au journal et gardée dans la fiche.

### Nouveautés de la version 1.18

- **Companions amis** : bouton « + Ami » (Messagerie → Messages directs) pour ajouter un companion à partir de sa carte de contact « meshcore://… » ou de sa clé publique complète + son nom ; bouton « ☆ Ajouter aux amis / ★ Ami » dans chaque conversation directe ; les amis sont en tête de liste et repérés en doré sur la carte.
- **Découvrir les voisins** dans la fiche d'un répéteur (commande discover.neighbors), console distante, étalonnage de la batterie du companion.

### Nouveautés de la version 1.22

- Paramètres d'un canal (bouton « ⚙ Paramètres » de la conversation), comme dans l'application MeshCore : partage par QR code (lien meshcore://channel/add avec clé secrète et périmètre de région), notifications (tous / mentions / aucune), rétention des messages (1 h à 30 jours), expéditeurs bloqués.
- « Participants du canal » (👥 nombre · ✉ messages) avec bouton « Bloquer / Débloquer ».
- Canal ADRASEC 06 : périmètre de région « fr-06 » appliqué par défaut (réglage de l'application MeshCore).
- Ajout d'un contact par lien « meshcore://contact/add?… » (QR code de l'application).

### Nouveautés de la version 1.21.2

- « Stations du canal » : registre des membres autorisés (stations à qui la clé a été remise), tenu par l'opérateur ; les stations qui écrivent sur le canal sans y figurer sont signalées ⚠, avec un bouton « Autoriser ».

### Nouveautés de la version 1.21.1

- Messagerie : bouton « Stations du canal » — liste des stations qui ont écrit sur le canal (nombre de messages, dernier message, réception, contact connu), avec « Message » ou « + Ajouter ».

### Nouveautés de la version 1.21

- Ajout d'un companion ami simplifié : on tape son nom ou indicatif, le logiciel retrouve sa clé publique dans les annonces entendues (nom comparé sans emoji ni majuscules) ; s'il n'a pas encore été entendu, il est ajouté automatiquement à sa prochaine annonce.
- La clé publique complète du companion est affichée (avec « Copier ») en tête de sa conversation.

### Nouveautés de la version 1.20.6

- Console distante : chaque étape est affichée et notée dans supervision.log, délais maximum sur chaque échange, détection d'une liaison bloquée avec le nœud companion.

### Nouveautés de la version 1.20.5

- Ligne à vue : correction de l'erreur « math domain error » (arrondi au dernier point du profil).

### Nouveautés de la version 1.20.4

- Console distante : suivi en direct de l'attente (secondes écoulées et étape en cours), bouton « Annuler », message clair si une commande est déjà en cours ; « neighbors » lu par la requête dédiée (la réponse texte était trop longue).

### Nouveautés de la version 1.20.3

- Messages reçus affichés en unités françaises : °F → °C, mph → km/h, miles → km, pouces → mm, pieds → m, inHg → hPa, vent « E6G14 » → « E 10 km/h (rafales 23 km/h) », H/L → max/min. Le texte d'origine reste visible dans le détail du message.

### Nouveautés de la version 1.20.2

- Position des nœuds : la carte suit la position GPS de la télémétrie, sinon celle annoncée par le nœud (sauf position saisie à la main). La fiche montre les coordonnées (carte, GPS, annonce), l'écart, et un bouton « Placer sur la carte ici ».
- Clé publique complète affichée dans chaque fiche, avec un bouton « Copier ».

### Nouveautés de la version 1.20.1

- Boutons « Advert » (voisins directs) et « Advert inondation » (tout le réseau) dans « Liaison avec le nœud » : annonce de votre nœud companion.

### Nouveautés de la version 1.20

- Liste des contacts du nœud companion triable et filtrable (Analyser mon nœud → Contacts → « Voir et trier ») : par type, nom, dernière annonce, distance ou route, avec recherche.
- Messagerie : section « ★ Prioritaires » en tête, avec le canal ADRASEC 06 et les messages des amis.
- Ligne à vue (Statistiques & couverture) : profil du terrain entre un point et chaque répéteur, courbure de la Terre et zone de Fresnel à 869,5 MHz (altitudes Copernicus via Open-Meteo, connexion Internet nécessaire).

### Nouveautés de la version 1.19.2

- Annulation de la 1.19.1 : retour au fonctionnement de la 1.19.0 pour la reconnaissance des canaux privés.

### Nouveautés de la version 1.19

- **Régions MeshCore** (portée des messages) : région choisie pour chaque canal dans la Messagerie (par défaut du nœud, aucune, #fr, #paca, #06, régions lues sur les répéteurs, ou autre), région par défaut du nœud companion et lecture des régions acceptées par les répéteurs ADRASEC (Réglages → Régions).
- Canaux : 🔒 canal privé (clé secrète), canal public (#) ou Public indiqués ; tous les canaux du nœud sont lus (plus seulement les 8 premiers).
- Ajout aux contacts depuis le détail d'un message (clé retrouvée dans les annonces entendues) ; liste des nœuds triée par dernier contact.

### Les données

Tout est enregistré sur le PC dans `%APPDATA%\ADRASEC06-Supervision\supervision.db`, y compris les mots de passe des répéteurs et le PIN Bluetooth (en clair, dans ce fichier local uniquement). Le journal technique est dans `supervision.log` au même endroit.

Pour partager l'état du réseau avec un autre opérateur : **Exporter les données** (fichier `.json`), puis **Importer un export** sur l'autre PC. Les mots de passe ne sont jamais exportés.

## Compiler l'exécutable

### Par GitHub Actions (comme CAT Pilot)

Poussez ce dossier dans un dépôt GitHub. Le workflow `.github/workflows/build.yml` compile l'exécutable à chaque `push` sur `main`. Récupérez-le dans l'onglet **Actions** → dernière exécution → artefact *Supervision-ADRASEC06-Windows*. Un tag `v1.0.0` crée en plus une release avec le `.exe`.

### Sur un PC Windows

Installez Python 3.12 (case « Add python.exe to PATH » cochée), puis double-cliquez `build.bat`. L'exécutable est créé dans `dist\`.

Pour tester sans compiler : `lancer-sans-compiler.bat`.

## Structure

```
run.py                   point d'entrée
supervision/app.py       fenêtre et fonctions appelées par l'interface
supervision/poller.py    lecture du réseau via le nœud companion USB ou Bluetooth (bibliothèque meshcore)
supervision/store.py     base locale SQLite
supervision/ui/          interface (HTML/JS)
supervision/seed.json    données initiales du réseau
supervision.spec         configuration PyInstaller
```

## Licence

Supervision MeshCore ADRASEC 06 — Copyright © 2026 Jean-Marie Bouthors (F4JCF).

Ce logiciel est libre : vous pouvez le redistribuer et le modifier selon les termes de la
[licence publique générale GNU, version 3](LICENSE) (GPL-3.0) ou, à votre choix, toute version
ultérieure. Il est fourni sans aucune garantie. Toute version modifiée et redistribuée doit
rester sous la même licence, avec ses sources.
