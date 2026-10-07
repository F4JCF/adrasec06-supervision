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
