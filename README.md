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
