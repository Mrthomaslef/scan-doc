#!/usr/bin/env python3
"""Génère data/drive_catalog.json et data/embedded_data.js (structure des dossiers uniquement, aucune donnée personnelle) pour fonctionnement hors-ligne et file://."""
import json
from pathlib import Path
import scan_daemon

ROOT = Path("/Users/thomaslefevre/.t3/projects/scan-doc")
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    payload = {
        "generatedAt": scan_daemon.datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "driveFolders": scan_daemon.list_all_drive_folders(),
        "vaultCategories": scan_daemon.VAULT_CATEGORIES,
        "inboxItems": [],
        "historyItems": [],
    }
    (DATA_DIR / "drive_catalog.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    js_content = (
        "// Auto-generated offline folder catalog for ScanDoc AI\n"
        f"window.SCANDOC_EMBEDDED_DATA = {json.dumps(payload, ensure_ascii=False)};\n"
    )
    (DATA_DIR / "embedded_data.js").write_text(js_content, encoding="utf-8")
    print(f"Built clean data/embedded_data.js ({len(payload['driveFolders'])} Drive folders)")


if __name__ == "__main__":
    main()
