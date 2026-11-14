<p align="center">
  <img src="assets/icon.svg" width="96" height="96" alt="ScanDoc AI">
</p>

# ScanDoc AI — Scanner iPhone vers Mon Drive

Application web mobile-first (PWA iOS & macOS) permettant de numériser des documents papier avec l'appareil photo de l'iPhone (zéro filigrane), de les redresser et améliorer automatiquement (**Couleur Magique**, **Noir & Blanc**, **Gris Doux**), d'extraire le texte par **Apple Vision OCR**, de générer **3 propositions de nommage intelligent** et de les classer directement dans le bon sous-dossier de **Mon Drive** (et en option dans le Coffre-Fort **AssurImpôts**).

## Fonctionnalités
- **Capture Caméra iPhone & Multi-Pages** : Appareil photo natif iOS HD, visée live multi-pages avec guide A4 et import PDF/Photo.
- **Traitement Qualité Scanner** : Redressement automatique des 4 coins (`VNDetectDocumentSegmentationRequest` + `CIPerspectiveCorrection`) et filtres scanner sans filigrane.
- **Renommage Intelligent & Classement Auto** : Détection de la date du document, de l'organisme émetteur et du dossier cible parmi les 38 dossiers de `Mon Drive`.
- **Séparation Multi-Documents** : Détecte automatiquement lorsqu'un scan multi-pages contient plusieurs contrats distincts et les sépare en PDF nommés.
- **Boîte de Tri Racine du Drive** : Analyse et range en 1 clic les fichiers déposés à la racine de `Mon Drive`.
