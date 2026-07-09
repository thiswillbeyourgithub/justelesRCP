#!/usr/bin/env bash
# Fetch the source datasets into ./data (gitignored). Both come from the public
# BDPM ("Base de données publique des médicaments") on data.gouv.fr.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data

# 1. RCP dump (Code_CIS <TAB> RCP_html). Large (~1GB uncompressed).
#    Provided as the "défi iDoc santé" export; see the dataset page:
#    https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante
RCP_URL="https://www.data.gouv.fr/api/1/datasets/r/bdbe2367-1898-4848-ac85-6fe58a1bdf68"

# 2. CIS -> drug name mapping (official denominations for titles + search).
#    TODO: confirm this resource id points to the current CIS_bdpm.txt export.
BDPM_URL="https://base-donnees-publique.medicaments.gouv.fr/telechargement.php?fichier=CIS_bdpm.txt"

if [ ! -f data/CIS_RCP.csv ]; then
  echo "Downloading RCP dump..."
  wget -O data/CIS_RCP.zip "$RCP_URL"
  echo "Unzip data/CIS_RCP.zip so that data/CIS_RCP.csv exists, then re-run build."
fi

if [ ! -f data/CIS_bdpm.txt ]; then
  echo "Downloading CIS_bdpm.txt..."
  wget -O data/CIS_bdpm.txt "$BDPM_URL" || \
    echo "WARN: CIS_bdpm.txt download failed; build will fall back to HTML denominations."
fi

echo "Done. Now run: uv run build.py"
