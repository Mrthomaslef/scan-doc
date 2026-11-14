#!/usr/bin/env python3
"""
ScanDoc AI Daemon — Serveur Local + Tunnel Cloudflare pour Scanner iPhone -> Google Drive & Coffre-Fort
Port local : 8794
Fonctionnalités :
1. Réception des photos scannées depuis l'iPhone (Caméra live / Photo / PDF).
2. Détection des bords, redressement de perspective, filtre scanner (Couleur Magique, N&B, Gris)
   et OCR haute précision via le binaire compilé `bin/vision_scanner` (Apple Vision + CoreImage + PDFKit).
3. Analyse sémantique via Gemini 2.5 Flash (clé dans t3-vault `gemini-api`) + fallback local :
   - Extraction de la date réelle du document, de l'organisme émetteur, du type de pièce et du résumé.
   - Génération de 3 conventions de nommage propres (Standard chronologique, Officiel MAJUSCULES, Lisible).
   - Détection automatique des scans multi-documents (ex: Contrat ISPE + Contrat ASH dans le même PDF).
   - Sélection intelligente du sous-dossier exact dans `Mon Drive` (/Volumes/GamingSSD/gdrive)
     et de la catégorie correspondante dans le Coffre-Fort `AssurImpôts`.
4. Rangement automatique dans Google Drive + copie optionnelle dans AssurImpôts / iCloud Drive.
5. Boîte de tri ("Inbox Racine du Drive") pour analyser et ranger en 1 clic les fichiers posés à la racine.
"""

from __future__ import annotations

import base64
import datetime
import glob
import hashlib
import http.server
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT_DIR = Path("/Users/thomaslefevre/.t3/projects/scan-doc")
VISION_BIN = ROOT_DIR / "bin" / "vision_scanner"
STAGING_DIR = Path("/tmp/scandoc_staging")
STAGING_DIR.mkdir(parents=True, exist_ok=True)

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = DATA_DIR / "scan_history.json"
INBOX_CACHE_FILE = DATA_DIR / "inbox_analysis_cache.json"
ENDPOINT_JSON = ROOT_DIR / "endpoint.json"

GDRIVE_ROOT = Path("/Volumes/GamingSSD/gdrive")
FALLBACK_GDRIVE = Path.home() / "Library" / "CloudStorage" / "GoogleDrive-blopdu94@gmail.com" / "Mon Drive"
VAULT_DOCS_DIR = Path("/Users/thomaslefevre/.t3/projects/projet-regroup/Documents_Importants")
ICLOUD_DOCS_DIR = Path("/Users/thomaslefevre/Library/Mobile Documents/com~apple~CloudDocs/Documents_Importants_Thomas_Lefevre")

PORT = int(os.environ.get("SCANDOC_PORT", "8794"))
PUBLIC_REPO = "Mrthomaslef/scan-doc"

ALLOWED_ORIGINS = {
    "https://mrthomaslef.github.io",
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
    "null",
}

# Catalogue structuré des dossiers principaux et sous-dossiers de Mon Drive (selon 00 - Plan du Drive.txt)
DRIVE_FOLDER_CATALOG = [
    {
        "path": "04 - Administratif & carrière/Poste assistant Cochin",
        "category": "04 - Administratif & carrière",
        "label": "Poste assistant Cochin (Contrats ASH, ISPE, RPPS, CDOM, Mutuelle)",
        "emoji": "🏥",
        "keywords": ["assistant", "ash", "ispe", "cochin", "agulhon", "feydy", "jubert", "rpps", "cdom", "ordre des medecins", "radioprotection", "casier", "contrat d'engagement", "service public exclusif", "aphp", "ap-hp"],
        "defaultVault": "02_Carriere_Medicale_APHP_Diplomes",
    },
    {
        "path": "04 - Administratif & carrière/Identité & banque",
        "category": "04 - Administratif & carrière",
        "label": "Identité, Banque, Impôts & Quotidien (CNI, Passeport, RIB, Contrats)",
        "emoji": "🪪",
        "keywords": ["passeport", "passport", "carte nationale", "cni", "identite", "iban", "rib", "credit mutuel", "credit agricole", "banque", "impot", "urssaf", "vitale", "navigo", "permis", "contrat de mariage", "etat civil", "naissance", "livret de famille", "assurance", "macif"],
        "defaultVault": "05_Banque_Finance_Impots_URSSAF",
    },
    {
        "path": "04 - Administratif & carrière/CV & candidatures",
        "category": "04 - Administratif & carrière",
        "label": "CV, Diplômes & Candidatures",
        "emoji": "🎓",
        "keywords": ["cv", "curriculum", "lettre de motivation", "candidature", "diplome", "attestation de reussite", "cursus", "des"],
        "defaultVault": "02_Carriere_Medicale_APHP_Diplomes",
    },
    {
        "path": "04 - Administratif & carrière/Cartes Avantage SNCF",
        "category": "04 - Administratif & carrière",
        "label": "SNCF, Transports & Billets",
        "emoji": "🚄",
        "keywords": ["sncf", "tgv", "carte avantage", "train", "air france", "billet", "reservation", "facture"],
        "defaultVault": "07_Voyages_Transports_Factures",
    },
    {
        "path": "01 - Cochin (en cours)/Interventions",
        "category": "01 - Cochin (en cours)",
        "label": "Cochin — Interventions RI, Comptes-Rendus & Suivi patients",
        "emoji": "💉",
        "keywords": ["intervention", "biopsie", "infiltration", "arthrographie", "vertebroplastie", "embolisation", "drainage", "radiofrequence", "cryoablation", "compte rendu operatoire", "cro", "consentement", "ordonnance"],
        "defaultVault": "",
    },
    {
        "path": "01 - Cochin (en cours)/Présentations de cas",
        "category": "01 - Cochin (en cours)",
        "label": "Cochin — Présentations de cas & Staffs / RCP",
        "emoji": "🩻",
        "keywords": ["staff", "rcp", "cas clinique", "presentation de cas", "dossier patient"],
        "defaultVault": "",
    },
    {
        "path": "01 - Cochin (en cours)/Cours & planning internes",
        "category": "01 - Cochin (en cours)",
        "label": "Cochin — Cours, Gardes & Planning des internes",
        "emoji": "📅",
        "keywords": ["planning", "interne", "garde", "astreinte", "semestre", "cours"],
        "defaultVault": "",
    },
    {
        "path": "02 - Bibliothèque radiologie/Radio Os",
        "category": "02 - Bibliothèque radiologie",
        "label": "Bibliothèque — Radiologie Ostéo-Articulaire (MSK, Cotten, SIMS)",
        "emoji": "🦴",
        "keywords": ["osteo", "msk", "cotten", "sims", "epaule", "hanche", "genou", "rachis", "poignet", "cheville", "arthro", "irm os"],
        "defaultVault": "",
    },
    {
        "path": "02 - Bibliothèque radiologie/Neuroradiologie interventionnelle (NRI PSL été 2026)",
        "category": "02 - Bibliothèque radiologie",
        "label": "Bibliothèque — Neuroradiologie Interventionnelle (NRI PSL)",
        "emoji": "🧠",
        "keywords": ["neuroradiologie", "nri", "anevrisme", "mav", "flow diverter", "thrombectomie", "psl", "pitie"],
        "defaultVault": "",
    },
    {
        "path": "02 - Bibliothèque radiologie/Radio autres",
        "category": "02 - Bibliothèque radiologie",
        "label": "Bibliothèque — Radiologie Générale (Neuro, Pédiatrie, Pneumo)",
        "emoji": "📚",
        "keywords": ["pneumo", "thorax", "pediatrie", "abdomen", "pelvis", "imagerie"],
        "defaultVault": "",
    },
    {
        "path": "03 - Recherche & publications/Ischio-jambiers (en cours)",
        "category": "03 - Recherche & publications",
        "label": "Recherche — Étude Ischio-jambiers",
        "emoji": "🏃",
        "keywords": ["ischio", "hamstring", "tendon", "sport"],
        "defaultVault": "03_These_Memoire_Recherche_Cours",
    },
    {
        "path": "03 - Recherche & publications/Article DWI b2500 (publication)",
        "category": "03 - Recherche & publications",
        "label": "Recherche — Article DWI b2500 Sein",
        "emoji": "🔬",
        "keywords": ["b2500", "dwi", "diffusion", "sein", "breast", "mri", "cerim"],
        "defaultVault": "03_These_Memoire_Recherche_Cours",
    },
    {
        "path": "05 - Perso & famille/Projet appartement",
        "category": "05 - Perso & famille",
        "label": "Perso & Famille — Appartement, Immobilier, Travaux & Charges",
        "emoji": "🏠",
        "keywords": ["appartement", "notaire", "syndic", "copropriete", "travaux", "devis", "loyer", "quittance", "edf", "engie", "taxe fonciere", "pret immobilier", "7 ter", "oudinot", "fontenay"],
        "defaultVault": "04_Immobilier_Logement_Travaux",
    },
    {
        "path": "05 - Perso & famille",
        "category": "05 - Perso & famille",
        "label": "Perso & Famille — Vie familiale, Marie, Émile, Santé & Factures",
        "emoji": "👨‍👩‍👦",
        "keywords": ["marie", "emile", "parizot", "famille", "creche", "mabrouka", "pajemploi", "pediatre", "vaccin", "carnet de sante", "mutuelle", "remboursement", "facture"],
        "defaultVault": "01_Identite_Etat_Civil_Famille",
    },
    {
        "path": "06 - Loisirs",
        "category": "06 - Loisirs",
        "label": "Loisirs — Culture, BD, Jeux & Divers",
        "emoji": "🎮",
        "keywords": ["loisir", "jeu", "pokemon", "bd", "livre", "philosophie", "art"],
        "defaultVault": "",
    },
    {
        "path": "99 - Archives/Internat - DES",
        "category": "99 - Archives",
        "label": "Archives — Internat, DES & Remplacements",
        "emoji": "📦",
        "keywords": ["internat", "remplacement", "sifem", "lariboisiere", "ancien"],
        "defaultVault": "02_Carriere_Medicale_APHP_Diplomes",
    },
]

VAULT_CATEGORIES = [
    {"id": "01_Identite_Etat_Civil_Famille", "label": "1. Identité, État Civil & Famille", "emoji": "🪪"},
    {"id": "02_Carriere_Medicale_APHP_Diplomes", "label": "2. Carrière Médicale, AP-HP & Diplômes", "emoji": "🏥"},
    {"id": "03_These_Memoire_Recherche_Cours", "label": "3. Thèse, Mémoire, Recherche & Protocoles", "emoji": "🔬"},
    {"id": "04_Immobilier_Logement_Travaux", "label": "4. Immobilier, Logement & Travaux", "emoji": "🏠"},
    {"id": "05_Banque_Finance_Impots_URSSAF", "label": "5. Banque, Finance, Impôts & URSSAF", "emoji": "🏦"},
    {"id": "06_Assurances_Mutuelle_Sante_Vehicule", "label": "6. Assurances, Mutuelle, Santé & Véhicule", "emoji": "🛡️"},
    {"id": "07_Voyages_Transports_Factures", "label": "7. Voyages, Transports & Factures d'Achats", "emoji": "🧾"},
]

current_tunnel_url = ""
_gemini_key_cache: str | None = None


def get_active_gdrive_root() -> Path:
    if GDRIVE_ROOT.exists():
        return GDRIVE_ROOT
    if FALLBACK_GDRIVE.exists():
        return FALLBACK_GDRIVE
    GDRIVE_ROOT.mkdir(parents=True, exist_ok=True)
    return GDRIVE_ROOT


def get_gemini_key() -> str:
    global _gemini_key_cache
    if _gemini_key_cache is not None:
        return _gemini_key_cache
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        _gemini_key_cache = env_key
        return env_key
    try:
        out = subprocess.check_output(
            ["/Users/thomaslefevre/.t3/bin/t3-vault", "get", "gemini-api"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        if out:
            _gemini_key_cache = out
            return out
    except Exception:
        pass
    _gemini_key_cache = ""
    return ""


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(items: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(items[:200], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def list_all_drive_folders() -> list[dict]:
    root = get_active_gdrive_root()
    known_by_path = {item["path"]: dict(item) for item in DRIVE_FOLDER_CATALOG}
    results = list(DRIVE_FOLDER_CATALOG)
    seen = set(known_by_path.keys())

    if root.exists():
        try:
            for top in sorted(root.iterdir()):
                if not top.is_dir() or top.name.startswith(".") or top.name in ("1.Fichiertpack",):
                    continue
                rel_top = top.name
                if rel_top not in seen:
                    seen.add(rel_top)
                    results.append({
                        "path": rel_top,
                        "category": rel_top,
                        "label": rel_top,
                        "emoji": "📁",
                        "keywords": [],
                        "defaultVault": "",
                    })
                for sub in sorted(top.iterdir()):
                    if not sub.is_dir() or sub.name.startswith("."):
                        continue
                    rel_sub = f"{top.name}/{sub.name}"
                    if rel_sub not in seen:
                        seen.add(rel_sub)
                        results.append({
                            "path": rel_sub,
                            "category": top.name,
                            "label": f"{top.name} › {sub.name}",
                            "emoji": "📂",
                            "keywords": [],
                            "defaultVault": "",
                        })
        except Exception:
            pass
    return results


def sanitize_filename(name: str, keep_spaces: bool = False) -> str:
    clean = name.strip()
    if clean.lower().endswith(".pdf"):
        clean = clean[:-4]
    # Replace forbidden path chars
    clean = re.sub(r"[/\\:*?\"<>|]+", "_", clean)
    if not keep_spaces:
        clean = re.sub(r"\s+", "_", clean)
        clean = re.sub(r"_+", "_", clean)
    else:
        clean = re.sub(r"\s+", " ", clean)
    clean = clean.strip(" _.-")
    if not clean:
        clean = f"{datetime.date.today().isoformat()}_Document_Scanne"
    return clean + ".pdf"


def strip_accents_ascii(text: str) -> str:
    replacements = {
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "à": "a", "â": "a", "ä": "a",
        "î": "i", "ï": "i",
        "ô": "o", "ö": "o",
        "ù": "u", "û": "u", "ü": "u",
        "ç": "c", "œ": "oe",
        "É": "E", "È": "E", "Ê": "E", "Ë": "E",
        "À": "A", "Â": "A", "Ä": "A",
        "Î": "I", "Ï": "I",
        "Ô": "O", "Ö": "O",
        "Ù": "U", "Û": "U", "Ü": "U",
        "Ç": "C",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text


def heuristic_analyze_ocr(ocr_text: str, page_count: int = 1, original_filename: str = "") -> dict:
    """Analyse locale déterministe ultra-rapide spécialisée pour le Drive du Dr Thomas Lefèvre."""
    text_lower = strip_accents_ascii(ocr_text.lower())
    today_str = datetime.date.today().isoformat()

    # 1. Extract date from French text (ex: "Fait à : PARIS\nle : 06/11/2026", "01 NOVEMBRE 2026", "2026-11-14")
    doc_date = today_str
    # Look for signature date ("Fait à ... le : DD/MM/YYYY" or "le : DD/MM/YYYY") first
    m_fait = re.search(r"(?:fait\s+[aà][\s\S]{0,40}?|le\s*:\s*)(\d{2})[/.-](\d{2})[/.-](202\d)", ocr_text, re.IGNORECASE)
    if m_fait:
        d, m, y = m_fait.group(1), m_fait.group(2), m_fait.group(3)
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            doc_date = f"{y}-{m}-{d}"
    else:
        m_iso = re.search(r"\b(202\d)-(\d{2})-(\d{2})\b", ocr_text)
        if m_iso:
            doc_date = f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}"
        else:
            # Find all DD/MM/202X dates (ignoring older law decrees like 20/03/2015)
            m_slashes = re.findall(r"\b(\d{2})/(\d{2})/(202\d)\b", ocr_text)
            valid_slashes = [(d, m, y) for (d, m, y) in m_slashes if 1 <= int(m) <= 12 and 1 <= int(d) <= 31]
            if valid_slashes:
                d, m, y = valid_slashes[-1]
                doc_date = f"{y}-{m}-{d}"
            else:
                months_fr = {
                    "janvier": "01", "fevrier": "02", "mars": "03", "avril": "04",
                    "mai": "05", "juin": "06", "juillet": "07", "aout": "08",
                    "septembre": "09", "octobre": "10", "novembre": "11", "decembre": "12"
                }
                # Ignore lines mentioning "decret" or "arrete"
                filtered_lines = [
                    ln for ln in text_lower.splitlines()
                    if "decret" not in ln and "arrete" not in ln and "loi " not in ln
                ]
                m_words = re.findall(
                    r"\b(\d{1,2})\s+(janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)\s+(202\d)\b",
                    "\n".join(filtered_lines),
                )
                if m_words:
                    day_str, month_name, year_str = m_words[-1]
                    doc_date = f"{year_str}-{months_fr[month_name]}-{int(day_str):02d}"

    yymm = doc_date[2:4] + doc_date[5:7]  # e.g. 2611 or MMYY
    mmyy = doc_date[5:7] + doc_date[2:4]  # e.g. 1126

    # 2. Detect multi-document scan (e.g. Contrat ISPE + Contrat ASH in the same PDF)
    split_suggestions = []
    pages_raw = re.split(r"---\s*Page\s+\d+\s*---", ocr_text)
    pages_content = [p.strip() for p in pages_raw if p.strip()]
    if len(pages_content) >= 2:
        has_ispe_p1 = "engagement de service public exclusif" in strip_accents_ascii(pages_content[0].lower()) and "echelon" not in strip_accents_ascii(pages_content[0].lower())
        has_ash_p2 = "assistant des hopitaux specialiste" in strip_accents_ascii(" ".join(pages_content[1:]).lower())
        if has_ispe_p1 and has_ash_p2:
            split_suggestions = [
                {
                    "pages": [1],
                    "title": "Contrat d'engagement ISPE (Service Public Exclusif)",
                    "filename": "LE_FEVRE_THOMAS_CT_ASH_ISPE_1126_SIGNE.pdf",
                    "standardFilename": f"{doc_date}_APHP_Cochin_Contrat_ISPE_Signe_Thomas_Lefevre.pdf",
                    "folder": "04 - Administratif & carrière/Poste assistant Cochin",
                },
                {
                    "pages": list(range(2, len(pages_content) + 1)),
                    "title": "Contrat d'engagement Assistant Spécialiste (ASH Cochin)",
                    "filename": "LE_FEVRE_THOMAS_CT_ASH_1126_SIGNE.pdf",
                    "standardFilename": f"{doc_date}_APHP_Cochin_Contrat_ASH_Specialiste_Signe_Thomas_Lefevre.pdf",
                    "folder": "04 - Administratif & carrière/Poste assistant Cochin",
                },
            ]

    # 3. Classify document type & destination
    is_signed = any(w in text_lower for w in ["lu et approuve", "lu el a", "signe", "signature", "le contractant"])
    sign_suffix = "_Signe" if is_signed else ""
    sign_upper = "_SIGNE" if is_signed else ""

    person = "Thomas Lefèvre"
    person_upper = "LE_FEVRE_THOMAS"
    if "marie" in text_lower and ("parizot" in text_lower or "grossesse" in text_lower or "pediatrie" in text_lower):
        person = "Marie Parizot"
        person_upper = "PARIZOT_MARIE"

    if "service public exclusif" in text_lower and "assistant des hopitaux specialiste" not in text_lower:
        doc_type = "Contrat ISPE (Service Public Exclusif)"
        issuer = "AP-HP Hôpital Cochin"
        folder = "04 - Administratif & carrière/Poste assistant Cochin"
        vault_cat = "02_Carriere_Medicale_APHP_Diplomes"
        std_name = f"{doc_date}_APHP_Cochin_Contrat_ISPE{sign_suffix}_Thomas_Lefevre.pdf"
        off_name = f"{person_upper}_CT_ASH_ISPE_{mmyy}{sign_upper}.pdf"
        read_name = f"Contrat ISPE Cochin{' Signé' if is_signed else ''} — {person} ({doc_date}).pdf"
        summary = "Contrat d'engagement de service public exclusif (ISPE) — AP-HP Hôpital Cochin."
    elif "assistant des hopitaux" in text_lower or "contrat d'engagement" in text_lower:
        doc_type = "Contrat Assistant des Hôpitaux (ASH)"
        issuer = "AP-HP Hôpital Cochin"
        folder = "04 - Administratif & carrière/Poste assistant Cochin"
        vault_cat = "02_Carriere_Medicale_APHP_Diplomes"
        std_name = f"{doc_date}_APHP_Cochin_Contrat_ASH{sign_suffix}_Thomas_Lefevre.pdf"
        off_name = f"{person_upper}_CT_ASH_{mmyy}{sign_upper}.pdf"
        read_name = f"Contrat Assistant Spécialiste Cochin{' Signé' if is_signed else ''} — {person} ({doc_date}).pdf"
        summary = "Contrat d'engagement Assistant Spécialiste des Hôpitaux (Radiologie B, Pr Feydy) — Hôpital Cochin."
    elif any(k in text_lower for k in ["rpps", "ordre des medecins", "cdom", "radioprotection", "casier", "fiche renseignement"]):
        doc_type = "Justificatif Administratif Prise de Poste"
        issuer = "AP-HP / Ordre des Médecins"
        folder = "04 - Administratif & carrière/Poste assistant Cochin"
        vault_cat = "02_Carriere_Medicale_APHP_Diplomes"
        std_name = f"{doc_date}_APHP_Cochin_Justificatif_Poste_Thomas_Lefevre.pdf"
        off_name = f"{person_upper}_JUSTIFICATIF_COCHIN_{mmyy}.pdf"
        read_name = f"Justificatif Poste Assistant Cochin — {person} ({doc_date}).pdf"
        summary = "Pièce justificative pour le dossier administratif d'Assistant des Hôpitaux à Cochin."
    elif any(k in text_lower for k in ["impot", "dgfip", "avis d'imposition", "taxe fonciere", "urssaf"]):
        doc_type = "Document Fiscal / URSSAF"
        issuer = "DGFiP / URSSAF"
        folder = "04 - Administratif & carrière/Identité & banque"
        vault_cat = "05_Banque_Finance_Impots_URSSAF"
        std_name = f"{doc_date}_Fiscalite_Impots_{person.replace(' ', '_')}.pdf"
        off_name = f"{person_upper}_IMPOTS_FISCALITE_{mmyy}.pdf"
        read_name = f"Document Fiscal / Impôts — {person} ({doc_date}).pdf"
        summary = "Avis ou justificatif fiscal / URSSAF."
    elif any(k in text_lower for k in ["iban", "releve d'identite bancaire", "credit mutuel", "credit agricole", "banque"]):
        doc_type = "RIB / Document Bancaire"
        issuer = "Banque"
        folder = "04 - Administratif & carrière/Identité & banque"
        vault_cat = "05_Banque_Finance_Impots_URSSAF"
        std_name = f"{doc_date}_Banque_RIB_{person.replace(' ', '_')}.pdf"
        off_name = f"{person_upper}_RIB_BANQUE_{mmyy}.pdf"
        read_name = f"Document Bancaire — {person} ({doc_date}).pdf"
        summary = "Relevé d'identité bancaire ou justificatif financier."
    elif any(k in text_lower for k in ["notaire", "syndic", "copropriete", "quittance", "loyer", "edf", "engie", "travaux", "devis"]):
        doc_type = "Document Appartement / Logement"
        issuer = "EDF" if "edf" in text_lower else "Engie" if "engie" in text_lower else "Notaire" if "notaire" in text_lower else "Syndic / Logement"
        sub_kind = "Facture_EDF" if "edf" in text_lower else "Facture_Engie" if "engie" in text_lower else "Quittance_Loyer" if "quittance" in text_lower else "Devis_Travaux" if "devis" in text_lower else "Acte_Notarie" if "notaire" in text_lower else "Appartement_Logement"
        folder = "05 - Perso & famille/Projet appartement"
        vault_cat = "04_Immobilier_Logement_Travaux"
        std_name = f"{doc_date}_{sub_kind}_{strip_accents_ascii(person).replace(' ', '_')}.pdf"
        off_name = f"{person_upper}_{sub_kind.upper()}_{mmyy}.pdf"
        read_name = f"{sub_kind.replace('_', ' ')} — {person} ({doc_date}).pdf"
        summary = f"Justificatif logement / appartement ({issuer}) pour {person}."
    elif any(k in text_lower for k in ["infiltration", "arthrographie", "biopsie", "compte rendu", "irm", "scanner", "echographie", "radiologie", "ordonnance"]):
        doc_type = "Document Médical / Radiologie Cochin"
        issuer = "Hôpital Cochin — Radiologie B"
        sub_kind = "Ordonnance" if "ordonnance" in text_lower else "CR_Intervention" if "intervention" in text_lower or "infiltration" in text_lower else "Compte_Rendu_Imagerie"
        folder = "01 - Cochin (en cours)/Interventions"
        vault_cat = ""
        std_name = f"{doc_date}_Cochin_{sub_kind}.pdf"
        off_name = f"COCHIN_{sub_kind.upper()}_{mmyy}.pdf"
        read_name = f"{sub_kind.replace('_', ' ')} Cochin ({doc_date}).pdf"
        summary = "Compte-rendu, ordonnance ou fiche d'intervention de Radiologie à Cochin."
    elif any(k in text_lower for k in ["facture", "recu", "ticket", "commande", "sncf", "train", "air france"]):
        doc_type = "Facture / Justificatif d'achat"
        issuer = "SNCF" if "sncf" in text_lower else "Air France" if "air france" in text_lower else "Commerce / Facture"
        sub_kind = "Billet_SNCF" if "sncf" in text_lower else "Billet_AirFrance" if "air france" in text_lower else "Facture_Achat"
        folder = "04 - Administratif & carrière/Cartes Avantage SNCF" if "sncf" in text_lower else "05 - Perso & famille"
        vault_cat = "07_Voyages_Transports_Factures"
        std_name = f"{doc_date}_{sub_kind}_{strip_accents_ascii(person).replace(' ', '_')}.pdf"
        off_name = f"{person_upper}_{sub_kind.upper()}_{mmyy}.pdf"
        read_name = f"{sub_kind.replace('_', ' ')} — {person} ({doc_date}).pdf"
        summary = f"Facture, billet ou justificatif ({issuer})."
    else:
        # Extract first clean title line from OCR text if available
        lines = [
            re.sub(r"[^a-zA-Z0-9À-ÿ\s'-]", " ", ln).strip()
            for ln in ocr_text.splitlines()
            if not ln.startswith("--- Page") and len(ln.strip()) >= 4
        ]
        title_cand = ""
        for ln in lines[:8]:
            words = [w for w in ln.split() if len(w) >= 2]
            if 1 <= len(words) <= 7 and not any(w.lower() in ("scanner", "made", "with", "page") for w in words):
                title_cand = " ".join(words[:5])
                break
        doc_type = title_cand or "Document Scanné"
        issuer = "Document Papier"
        folder = "04 - Administratif & carrière/Identité & banque"
        vault_cat = "01_Identite_Etat_Civil_Famille"
        base_hint = re.sub(r"[^a-zA-Z0-9]+", "_", strip_accents_ascii(title_cand or original_filename or "Document")).strip("_")
        if not base_hint or base_hint.lower() in ("image", "scan", "pdf", "blob"):
            base_hint = "Document_Scanne"
        std_name = f"{doc_date}_{base_hint}.pdf"
        off_name = f"{person_upper}_{base_hint.upper()}_{mmyy}.pdf"
        read_name = f"{base_hint.replace('_', ' ')} ({doc_date}).pdf"
        summary = "Document papier numérisé et prêt à être classé dans Mon Drive."

    return {
        "docType": doc_type,
        "docDate": doc_date,
        "issuer": issuer,
        "person": person,
        "isSigned": is_signed,
        "summary": summary,
        "suggestedNames": [
            {"id": "standard", "label": "Chronologique (Recommandé)", "filename": sanitize_filename(std_name)},
            {"id": "official", "label": "Officiel Administratif", "filename": sanitize_filename(off_name)},
            {"id": "readable", "label": "Clair & Lisible", "filename": sanitize_filename(read_name, keep_spaces=True)},
        ],
        "recommendedFolder": folder,
        "vaultCategory": vault_cat,
        "recommendVaultCopy": bool(vault_cat),
        "splitSuggestions": split_suggestions,
        "aiEngine": "Apple Vision OCR + Règles Métier Cochin/Drive",
    }


def analyze_with_gemini(ocr_text: str, page_count: int = 1, original_filename: str = "") -> dict:
    """Enrichit l'analyse OCR avec Gemini Flash ou Ollama qwen3:8b local si le document n'est pas déjà identifié avec certitude."""
    base = heuristic_analyze_ocr(ocr_text, page_count=page_count, original_filename=original_filename)
    if len(ocr_text.strip()) < 15:
        return base

    # Si nos règles métier ont déjà reconnu un contrat Cochin / ASH / ISPE avec certitude, réponse immédiate (<5ms)
    if "Contrat" in base["docType"] and "Cochin" in base["issuer"]:
        return base

    available_folders = [f["path"] for f in list_all_drive_folders()]
    vault_ids = [v["id"] for v in VAULT_CATEGORIES]

    prompt = f"""Tu es l'assistant personnel d'archivage du Dr Thomas Lefèvre (Radiologue — Assistant Spécialiste à l'Hôpital Cochin AP-HP, marié à Marie Parizot, père d'Émile).
Voici le texte OCR extrait d'un document papier de {page_count} page(s) (nom initial éventuel : "{original_filename}") :

\"\"\"
{ocr_text[:4000]}
\"\"\"

Dossiers disponibles dans son Google Drive (`Mon Drive`) :
{json.dumps(available_folders, ensure_ascii=False)}

Catégories disponibles dans son Coffre-Fort `AssurImpôts` (laisser vide "" si non pertinent) :
{json.dumps(vault_ids, ensure_ascii=False)}

Réponds UNIQUEMENT en JSON valide avec la structure exacte suivante :
{{
  "docType": "Type précis du document (ex: Contrat d'engagement ASH, Contrat ISPE, Facture EDF, Ordonnance, Quittance, Impôts...)",
  "docDate": "YYYY-MM-DD (date réelle du document trouvée dans le texte, sinon {base['docDate']})",
  "issuer": "Organisme émetteur court (ex: AP-HP Hôpital Cochin, EDF, DGFiP, Crédit Mutuel, SNCF...)",
  "person": "Personne concernée (ex: Thomas Lefèvre, Marie Parizot, Émile Lefèvre)",
  "isSigned": true,
  "summary": "Résumé très clair en 1 phrase (max 140 caractères)",
  "standardFilename": "YYYY-MM-DD_Organisme_Type_Précis_Sans_Espaces_Ni_Accents.pdf",
  "officialFilename": "LE_FEVRE_THOMAS_TYPE_COURT_MMYY_SIGNE.pdf",
  "readableFilename": "Titre Clair du Document — Personne (DD-MM-YYYY).pdf",
  "recommendedFolder": "Chemin exact parmi la liste des dossiers Google Drive ci-dessus",
  "vaultCategory": "ID exact parmi les catégories Coffre-Fort ou chaine vide",
  "recommendVaultCopy": true
}}"""

    parsed = None
    engine_label = base["aiEngine"]

    # 1. Try Gemini API if key starts with AIza
    api_key = get_gemini_key()
    if api_key.startswith("AIza"):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parsed = json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            engine_label = "Apple Vision OCR + Gemini 2.5 Flash"
        except Exception:
            parsed = None

    # 2. Try local Ollama qwen3:8b (free, private, on-device M4)
    if parsed is None:
        try:
            ollama_payload = {
                "model": "qwen3:8b",
                "think": False,
                "format": "json",
                "stream": False,
                "messages": [
                    {"role": "system", "content": "Tu es un classifieur documentaire JSON précis. Réponds uniquement en JSON valide."},
                    {"role": "user", "content": prompt},
                ],
            }
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/chat",
                data=json.dumps(ollama_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
            parsed = json.loads(res_data["message"]["content"])
            engine_label = "Apple Vision OCR + IA Locale Qwen3"
        except Exception:
            parsed = None

    if not parsed or not isinstance(parsed, dict):
        return base

    rec_folder = parsed.get("recommendedFolder") or base["recommendedFolder"]
    if rec_folder not in available_folders:
        rec_folder = base["recommendedFolder"]

    std_fn = sanitize_filename(parsed.get("standardFilename") or base["suggestedNames"][0]["filename"])
    off_fn = sanitize_filename(parsed.get("officialFilename") or base["suggestedNames"][1]["filename"])
    read_fn = sanitize_filename(parsed.get("readableFilename") or base["suggestedNames"][2]["filename"], keep_spaces=True)

    return {
        "docType": parsed.get("docType") or base["docType"],
        "docDate": parsed.get("docDate") or base["docDate"],
        "issuer": parsed.get("issuer") or base["issuer"],
        "person": parsed.get("person") or base["person"],
        "isSigned": bool(parsed.get("isSigned", base["isSigned"])),
        "summary": parsed.get("summary") or base["summary"],
        "suggestedNames": [
            {"id": "standard", "label": "Chronologique (Recommandé)", "filename": std_fn},
            {"id": "official", "label": "Officiel Administratif", "filename": off_fn},
            {"id": "readable", "label": "Clair & Lisible", "filename": read_fn},
        ],
        "recommendedFolder": rec_folder,
        "vaultCategory": parsed.get("vaultCategory", base["vaultCategory"]),
        "recommendVaultCopy": bool(parsed.get("recommendVaultCopy", base["recommendVaultCopy"])),
        "splitSuggestions": base["splitSuggestions"],
        "aiEngine": engine_label,
    }


def run_vision_ocr(file_path: Path) -> dict:
    try:
        res = subprocess.run(
            [str(VISION_BIN), "ocr", str(file_path)],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception as e:
        return {"ok": False, "pageCount": 1, "text": "", "error": str(e)}
    return {"ok": False, "pageCount": 1, "text": ""}


def run_vision_process(input_paths: list[Path], output_pdf: Path, filter_mode: str = "magic", autocrop: bool = True) -> dict:
    cmd = [
        str(VISION_BIN),
        "process",
        "--output", str(output_pdf),
        "--filter", filter_mode,
        "--autocrop", "1" if autocrop else "0",
        *[str(p) for p in input_paths],
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "Vision process failed"}


def split_pdf_pages(source_pdf: Path, pages_1based: list[int], dest_pdf: Path) -> bool:
    """Extrait une sélection de pages (1-based) d'un PDF via un petit script Swift/PDFKit ou Python."""
    swift_code = """
import Foundation
import PDFKit
let args = CommandLine.arguments
let src = URL(fileURLWithPath: args[1])
let dst = URL(fileURLWithPath: args[2])
let pages = args[3].split(separator: ",").compactMap { Int($0) }
guard let doc = PDFDocument(url: src) else { exit(1) }
let out = PDFDocument()
for p in pages {
    if p >= 1 && p <= doc.pageCount, let page = doc.page(at: p - 1) {
        out.insert(page, at: out.pageCount)
    }
}
try? FileManager.default.createDirectory(at: dst.deletingLastPathComponent(), withIntermediateDirectories: true)
exit(out.write(to: dst) ? 0 : 1)
"""
    pages_arg = ",".join(str(p) for p in pages_1based)
    try:
        res = subprocess.run(
            ["/usr/bin/swift", "-e", swift_code, str(source_pdf), str(dest_pdf), pages_arg],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return res.returncode == 0 and dest_pdf.is_file()
    except Exception:
        return False


def scan_drive_root_inbox() -> list[dict]:
    """Détecte les fichiers posés en vrac à la racine de Mon Drive et les analyse avec cache."""
    root = get_active_gdrive_root()
    if not root.exists():
        return []

    cache: dict = {}
    if INBOX_CACHE_FILE.exists():
        try:
            cache = json.loads(INBOX_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    allowed_exts = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp"}
    items = []
    cache_dirty = False

    for fpath in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not fpath.is_file() or fpath.name.startswith(".") or fpath.name == "Icon\r":
            continue
        if fpath.suffix.lower() not in allowed_exts:
            continue
        st = fpath.stat()
        cache_key = f"{fpath.name}:{int(st.st_mtime)}:{st.st_size}"
        if cache_key in cache:
            items.append(cache[cache_key])
            continue

        ocr_res = run_vision_ocr(fpath)
        ocr_text = ocr_res.get("text", "")
        page_count = ocr_res.get("pageCount", 1)
        analysis = analyze_with_gemini(ocr_text, page_count=page_count, original_filename=fpath.name)
        entry = {
            "name": fpath.name,
            "sizeKb": round(st.st_size / 1024, 1),
            "modifiedAt": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "pageCount": page_count,
            "ocrExcerpt": ocr_text[:500],
            "analysis": analysis,
        }
        cache[cache_key] = entry
        cache_dirty = True
        items.append(entry)

    if cache_dirty:
        try:
            INBOX_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return items


class ScanDocHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_cors(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range, Authorization, X-Scan-Key")
        self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges, Content-Disposition")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, status: int, payload: dict, head_only: bool = False) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._send_cors()
        self.end_headers()
        if not head_only:
            self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._handle_get(head_only=True)

    def do_GET(self) -> None:
        self._handle_get(head_only=False)

    def _serve_file(self, fpath: Path, download: bool = False, head_only: bool = False) -> None:
        if not fpath.is_file():
            self._send_json(404, {"ok": False, "error": "Fichier introuvable"}, head_only=head_only)
            return
        file_size = fpath.stat().st_size
        ctype, _ = mimetypes.guess_type(str(fpath))
        if not ctype:
            ctype = "application/pdf" if fpath.suffix.lower() == ".pdf" else "application/octet-stream"

        disp = "attachment" if download else "inline"
        safe_name = urllib.parse.quote(fpath.name)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{safe_name}")
        self._send_cors()
        self.end_headers()
        if head_only:
            return
        with open(fpath, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def _handle_get(self, head_only: bool = False) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/api/status":
            root = get_active_gdrive_root()
            inbox_files = []
            drive_mounted = False
            try:
                drive_mounted = root.exists()
                if drive_mounted:
                    inbox_files = [
                        p.name for p in root.iterdir()
                        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in {".pdf", ".jpg", ".jpeg", ".png", ".heic"}
                    ]
            except Exception:
                pass
            history = load_history()
            self._send_json(200, {
                "ok": True,
                "service": "scandoc-ai",
                "driveMounted": drive_mounted,
                "driveRoot": str(root),
                "tunnelUrl": current_tunnel_url,
                "inboxCount": len(inbox_files),
                "historyCount": len(history),
            }, head_only=head_only)
            return

        if path == "/api/folders":
            self._send_json(200, {
                "ok": True,
                "driveFolders": list_all_drive_folders(),
                "vaultCategories": VAULT_CATEGORIES,
            }, head_only=head_only)
            return

        if path == "/api/inbox":
            items = scan_drive_root_inbox()
            self._send_json(200, {"ok": True, "count": len(items), "items": items}, head_only=head_only)
            return

        if path == "/api/history":
            self._send_json(200, {"ok": True, "items": load_history()}, head_only=head_only)
            return

        if path == "/api/staging":
            scan_id = re.sub(r"[^a-zA-Z0-9_-]", "", (query.get("id", [""])[0] or ""))
            target = STAGING_DIR / f"{scan_id}.pdf"
            dl = (query.get("download", ["0"])[0] == "1")
            self._serve_file(target, download=dl, head_only=head_only)
            return

        if path == "/api/file":
            rel = (query.get("path", [""])[0] or "").strip().lstrip("/")
            if ".." in Path(rel).parts:
                self._send_json(400, {"ok": False, "error": "Chemin invalide"}, head_only=head_only)
                return
            root = get_active_gdrive_root()
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                self._send_json(403, {"ok": False, "error": "Accès refusé"}, head_only=head_only)
                return
            dl = (query.get("download", ["0"])[0] == "1")
            self._serve_file(candidate, download=dl, head_only=head_only)
            return

        # Serve static PWA files
        rel_static = "index.html" if path in ("/", "") else path.lstrip("/")
        if ".." not in Path(rel_static).parts:
            static_target = (ROOT_DIR / rel_static).resolve()
            try:
                static_target.relative_to(ROOT_DIR.resolve())
                if static_target.is_file():
                    self._serve_file(static_target, download=False, head_only=head_only)
                    return
            except ValueError:
                pass

        self._send_json(404, {"ok": False, "error": "Not found"}, head_only=head_only)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 80 * 1024 * 1024:
                self._send_json(413, {"ok": False, "error": "Fichier trop volumineux (max 80 Mo)"})
                return
            body = json.loads(self.rfile.read(length) or b"{}") if length > 0 else {}
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Requête JSON invalide: {e}"})
            return

        if path == "/api/analyze":
            self._handle_analyze(body)
            return
        if path == "/api/save":
            self._handle_save(body)
            return
        if path == "/api/split-save":
            self._handle_split_save(body)
            return
        if path == "/api/inbox/file":
            self._handle_inbox_file(body)
            return

        self._send_json(404, {"ok": False, "error": "Endpoint inconnu"})

    def _decode_data_urls_to_files(self, data_urls: list[str], scan_id: str) -> list[Path]:
        saved_paths: list[Path] = []
        for idx, durl in enumerate(data_urls):
            if not durl:
                continue
            ext = ".jpg"
            b64_part = durl
            if durl.startswith("data:"):
                header, _, b64_part = durl.partition(",")
                if "application/pdf" in header:
                    ext = ".pdf"
                elif "image/png" in header:
                    ext = ".png"
                elif "image/webp" in header:
                    ext = ".webp"
            raw_bytes = base64.b64decode(b64_part)
            out_file = STAGING_DIR / f"{scan_id}_input_{idx + 1}{ext}"
            out_file.write_bytes(raw_bytes)
            saved_paths.append(out_file)
        return saved_paths

    def _handle_analyze(self, body: dict) -> None:
        scan_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("scanId") or "")) or f"scan_{int(time.time() * 1000)}"
        filter_mode = str(body.get("filter") or "magic")
        autocrop = bool(body.get("autocrop", True))
        original_filename = str(body.get("originalFilename") or "")
        images = body.get("images") or []

        if not images:
            self._send_json(400, {"ok": False, "error": "Aucune image ou page fournie"})
            return

        input_paths = self._decode_data_urls_to_files(images, scan_id)
        if not input_paths:
            self._send_json(400, {"ok": False, "error": "Impossible de décoder les images"})
            return

        out_pdf = STAGING_DIR / f"{scan_id}.pdf"
        proc_res = run_vision_process(input_paths, out_pdf, filter_mode=filter_mode, autocrop=autocrop)
        if not proc_res.get("ok"):
            self._send_json(500, {"ok": False, "error": proc_res.get("error") or "Échec du traitement Vision"})
            return

        ocr_text = proc_res.get("text") or ""
        page_count = proc_res.get("pageCount") or len(input_paths)
        size_kb = round((proc_res.get("sizeBytes") or 0) / 1024, 1)

        analysis = analyze_with_gemini(ocr_text, page_count=page_count, original_filename=original_filename)
        self._send_json(200, {
            "ok": True,
            "scanId": scan_id,
            "pageCount": page_count,
            "sizeKb": size_kb,
            "ocrText": ocr_text,
            "previewUrl": f"/api/staging?id={scan_id}",
            "analysis": analysis,
        })

    def _handle_save(self, body: dict) -> None:
        scan_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("scanId") or ""))
        filename = sanitize_filename(str(body.get("filename") or "Document_Scanne.pdf"), keep_spaces=bool(body.get("keepSpaces", False)))
        drive_folder = str(body.get("driveFolder") or "04 - Administratif & carrière/Identité & banque").strip().strip("/")
        new_subfolder = re.sub(r"[/\\:*?\"<>|]+", "-", str(body.get("newSubfolder") or "").strip()).strip(" .-")
        copy_to_vault = bool(body.get("copyToVault", False))
        vault_category = str(body.get("vaultCategory") or "").strip()
        doc_type = str(body.get("docType") or "Document scanné")
        doc_date = str(body.get("docDate") or datetime.date.today().isoformat())
        summary = str(body.get("summary") or "")

        if ".." in Path(drive_folder).parts:
            self._send_json(400, {"ok": False, "error": "Dossier Drive invalide"})
            return

        staging_pdf = STAGING_DIR / f"{scan_id}.pdf"
        ocr_text_from_rebuild = ""
        page_count_from_rebuild = 1
        if not staging_pdf.is_file():
            # Rebuild from images if provided
            images = body.get("images") or []
            if not images:
                self._send_json(404, {"ok": False, "error": "PDF temporaire expiré, veuillez relancer l'analyse"})
                return
            scan_id = f"scan_{int(time.time() * 1000)}"
            staging_pdf = STAGING_DIR / f"{scan_id}.pdf"
            input_paths = self._decode_data_urls_to_files(images, scan_id)
            proc_res = run_vision_process(input_paths, staging_pdf, filter_mode=str(body.get("filter") or "magic"), autocrop=bool(body.get("autocrop", True)))
            ocr_text_from_rebuild = proc_res.get("text") or ""
            page_count_from_rebuild = proc_res.get("pageCount") or len(input_paths)

        if not staging_pdf.is_file():
            self._send_json(500, {"ok": False, "error": "Impossible de générer le PDF final"})
            return

        # Si l'utilisateur a cliqué "Ranger" avant la fin de l'analyse (nom générique *_Document.pdf),
        # on calcule automatiquement le vrai nom intelligent et le bon dossier côté serveur !
        if re.match(r"^(\d{4}-\d{2}-\d{2}_)?document(_scanne)?\.pdf$", filename.lower()):
            if not ocr_text_from_rebuild:
                ocr_res = run_vision_ocr(staging_pdf)
                ocr_text_from_rebuild = ocr_res.get("text") or ""
                page_count_from_rebuild = ocr_res.get("pageCount") or 1
            auto_analysis = analyze_with_gemini(ocr_text_from_rebuild, page_count=page_count_from_rebuild)
            if auto_analysis.get("suggestedNames"):
                filename = auto_analysis["suggestedNames"][0]["filename"]
            if auto_analysis.get("recommendedFolder"):
                drive_folder = auto_analysis["recommendedFolder"]
            doc_type = auto_analysis.get("docType") or doc_type
            doc_date = auto_analysis.get("docDate") or doc_date
            summary = auto_analysis.get("summary") or summary
            if auto_analysis.get("vaultCategory"):
                vault_category = auto_analysis["vaultCategory"]
                copy_to_vault = True

        root = get_active_gdrive_root()
        target_dir = root / drive_folder
        if new_subfolder:
            target_dir = target_dir / new_subfolder
            drive_folder = f"{drive_folder}/{new_subfolder}"
        target_dir.mkdir(parents=True, exist_ok=True)

        dest_path = target_dir / filename
        if dest_path.exists():
            stem = dest_path.stem
            dest_path = target_dir / f"{stem}_{datetime.datetime.now().strftime('%H%M%S')}.pdf"
            filename = dest_path.name

        shutil.copy2(staging_pdf, dest_path)
        size_kb = round(dest_path.stat().st_size / 1024, 1)

        vault_saved_path = ""
        if copy_to_vault and vault_category:
            for base_v in (VAULT_DOCS_DIR, ICLOUD_DOCS_DIR):
                try:
                    if base_v.exists():
                        vdir = base_v / vault_category
                        vdir.mkdir(parents=True, exist_ok=True)
                        vdest = vdir / filename
                        shutil.copy2(dest_path, vdest)
                        if not vault_saved_path:
                            vault_saved_path = f"{vault_category}/{filename}"
                except Exception:
                    pass

        rel_drive_path = f"{drive_folder}/{filename}"
        history = load_history()
        record = {
            "id": f"h_{int(time.time() * 1000)}",
            "filename": filename,
            "driveFolder": drive_folder,
            "driveRelPath": rel_drive_path,
            "vaultPath": vault_saved_path,
            "docType": doc_type,
            "docDate": doc_date,
            "summary": summary,
            "sizeKb": size_kb,
            "createdAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        history.insert(0, record)
        save_history(history)

        self._send_json(200, {
            "ok": True,
            "record": record,
        })

    def _handle_split_save(self, body: dict) -> None:
        """Sépare un scan ou un fichier de la racine en plusieurs PDF distincts et les range dans le Drive."""
        scan_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(body.get("scanId") or ""))
        inbox_name = str(body.get("inboxName") or "").strip()
        splits = body.get("splits") or []
        remove_inbox_source = bool(body.get("removeInboxSource", False))

        root = get_active_gdrive_root()
        if inbox_name:
            source_pdf = (root / Path(inbox_name).name).resolve()
        else:
            source_pdf = STAGING_DIR / f"{scan_id}.pdf"

        if not source_pdf.is_file():
            self._send_json(404, {"ok": False, "error": "Document source introuvable"})
            return

        saved_records = []
        history = load_history()

        for sp in splits:
            pages = sp.get("pages") or [1]
            fname = sanitize_filename(str(sp.get("filename") or "Document_Extrait.pdf"))
            folder = str(sp.get("folder") or "04 - Administratif & carrière/Poste assistant Cochin").strip("/")
            target_dir = root / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            dest_pdf = target_dir / fname
            if split_pdf_pages(source_pdf, pages, dest_pdf):
                size_kb = round(dest_pdf.stat().st_size / 1024, 1)
                rec = {
                    "id": f"h_{int(time.time() * 1000)}_{len(saved_records)}",
                    "filename": fname,
                    "driveFolder": folder,
                    "driveRelPath": f"{folder}/{fname}",
                    "vaultPath": "",
                    "docType": str(sp.get("title") or "Document séparé"),
                    "docDate": datetime.date.today().isoformat(),
                    "summary": f"Pages {pages} extraites et classées automatiquement.",
                    "sizeKb": size_kb,
                    "createdAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                saved_records.append(rec)
                history.insert(0, rec)

        if saved_records and remove_inbox_source and inbox_name and source_pdf.parent == root:
            try:
                arch_dir = root / "99 - Archives" / "Divers"
                arch_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source_pdf), str(arch_dir / source_pdf.name))
            except Exception:
                pass

        save_history(history)
        self._send_json(200, {"ok": True, "saved": saved_records})

    def _handle_inbox_file(self, body: dict) -> None:
        """Renomme et déplace un fichier présent à la racine de Mon Drive vers son bon sous-dossier."""
        source_name = Path(str(body.get("sourceName") or "")).name
        new_filename = sanitize_filename(str(body.get("filename") or source_name), keep_spaces=bool(body.get("keepSpaces", False)))
        drive_folder = str(body.get("driveFolder") or "04 - Administratif & carrière/Poste assistant Cochin").strip().strip("/")
        copy_to_vault = bool(body.get("copyToVault", False))
        vault_category = str(body.get("vaultCategory") or "").strip()
        doc_type = str(body.get("docType") or "Document Drive")
        doc_date = str(body.get("docDate") or datetime.date.today().isoformat())
        summary = str(body.get("summary") or "")

        root = get_active_gdrive_root()
        src_path = root / source_name
        if not src_path.is_file():
            self._send_json(404, {"ok": False, "error": f"Fichier {source_name} introuvable à la racine du Drive"})
            return

        target_dir = root / drive_folder
        target_dir.mkdir(parents=True, exist_ok=True)
        dest_path = target_dir / new_filename
        if dest_path.exists() and dest_path.resolve() != src_path.resolve():
            dest_path = target_dir / f"{dest_path.stem}_{datetime.datetime.now().strftime('%H%M%S')}{dest_path.suffix}"
            new_filename = dest_path.name

        shutil.move(str(src_path), str(dest_path))
        size_kb = round(dest_path.stat().st_size / 1024, 1)

        vault_saved_path = ""
        if copy_to_vault and vault_category:
            for base_v in (VAULT_DOCS_DIR, ICLOUD_DOCS_DIR):
                try:
                    if base_v.exists():
                        vdir = base_v / vault_category
                        vdir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dest_path, vdir / new_filename)
                        if not vault_saved_path:
                            vault_saved_path = f"{vault_category}/{new_filename}"
                except Exception:
                    pass

        history = load_history()
        record = {
            "id": f"h_{int(time.time() * 1000)}",
            "filename": new_filename,
            "driveFolder": drive_folder,
            "driveRelPath": f"{drive_folder}/{new_filename}",
            "vaultPath": vault_saved_path,
            "docType": doc_type,
            "docDate": doc_date,
            "summary": summary,
            "sizeKb": size_kb,
            "createdAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        history.insert(0, record)
        save_history(history)

        self._send_json(200, {"ok": True, "record": record})

    def log_message(self, format: str, *args) -> None:
        pass


def save_and_push_endpoint(tunnel_url: str) -> None:
    global current_tunnel_url
    current_tunnel_url = tunnel_url
    payload = {
        "tunnelUrl": tunnel_url,
        "localPort": PORT,
        "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    ENDPOINT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    gh = "/opt/homebrew/bin/gh"
    api = f"repos/{PUBLIC_REPO}/contents/endpoint.json"
    try:
        cur = subprocess.run([gh, "api", api, "--jq", ".sha"], capture_output=True, text=True, timeout=20)
        sha = cur.stdout.strip() if cur.returncode == 0 else ""
        content = base64.b64encode((json.dumps(payload, indent=2) + "\n").encode()).decode()
        args = [gh, "api", "-X", "PUT", api, "-f", "message=chore(tunnel): update scandoc endpoint", "-f", f"content={content}"]
        if sha:
            args += ["-f", f"sha={sha}"]
        subprocess.run(args, capture_output=True, text=True, timeout=25)
    except Exception:
        pass


def run_tunnel_forever() -> None:
    cf_candidates = glob.glob(os.path.expanduser("~/.t3/tools/cloudflared/*/*/cloudflared"))
    if not cf_candidates:
        cf_candidates = glob.glob("/Users/thomaslefevre/.t3/projects/*/.bin/cloudflared")
    if not cf_candidates:
        return
    cf_bin = cf_candidates[0]
    while True:
        try:
            proc = subprocess.Popen(
                [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    url = m.group(0)
                    print(f"[scandoc] Active Tunnel URL: {url}", flush=True)
                    save_and_push_endpoint(url)
            proc.wait()
        except Exception as e:
            print(f"[scandoc] Tunnel error: {e}", flush=True)
        time.sleep(5)


def main() -> None:
    threading.Thread(target=run_tunnel_forever, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), ScanDocHandler)
    print(f"ScanDoc AI Server listening on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
