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
#    Official BDPM export, shipped as a zip of tab-separated latin-1 CSVs; we
#    only need CIS_bdpm.csv from it (Code_CIS <TAB> Denomination <TAB> ...),
#    which build.py reads as data/CIS_bdpm.txt.
#    Dataset page: https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante
BDPM_URL="https://www.data.gouv.fr/api/1/datasets/r/fecf69dd-ca9f-4902-95dd-4e0ec6ab92f0"

if [ ! -f data/CIS_RCP.csv ]; then
  echo "Downloading RCP dump..."
  wget -O data/CIS_RCP.zip "$RCP_URL"
  echo "Unzip data/CIS_RCP.zip so that data/CIS_RCP.csv exists, then re-run build."
fi

if [ ! -f data/CIS_bdpm.txt ]; then
  echo "Downloading CIS_bdpm zip..."
  if wget -O data/CIS_bdpm.zip "$BDPM_URL"; then
    # The archive holds several *_bdpm.csv files; we only want CIS_bdpm.csv,
    # which matches the layout build.py expects for data/CIS_bdpm.txt.
    unzip -p data/CIS_bdpm.zip CIS_bdpm.csv > data/CIS_bdpm.txt
    rm -f data/CIS_bdpm.zip
  else
    echo "WARN: CIS_bdpm download failed; build will fall back to HTML denominations."
  fi
fi

echo "Done. Now run: uv run build.py"
