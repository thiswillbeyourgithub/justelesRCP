#!/usr/bin/env bash
# Fetch the source datasets into ./data (gitignored). Both come from the public
# BDPM ("Base de données publique des médicaments") on data.gouv.fr.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data

# 1. RCP dump (Code_CIS <TAB> RCP_html). Large (~1GB uncompressed).
#    Provided as the "défi iDoc santé" export; see the dataset page:
#    https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante
#    NOTE: this is a FROZEN snapshot from 2 May 2022 and the only bulk dump of RCP
#    *HTML* that exists (the official BDPM download ships metadata only). It serves
#    as a baseline floor. To refresh individual RCPs from the live ANSM site, run
#    scrape-rcp.py, which writes per-CIS overlay files that build.py prefers over
#    this baseline (see its module docstring). This dump is optional if you scrape.
RCP_URL="https://www.data.gouv.fr/api/1/datasets/r/bdbe2367-1898-4848-ac85-6fe58a1bdf68"

# 2. CIS -> drug name mapping (official denominations for titles + search), plus
#    two optional joins that scrape-rcp.py uses to match drugs to the frequency
#    list via their active substance / reference brand (never to build pages).
#    All three ship in the SAME BDPM zip (tab-separated latin-1 CSVs):
#      CIS_bdpm.csv       -> data/CIS_bdpm.txt        (Code_CIS <TAB> Denomination <TAB> ...)
#      CIS_COMPO_bdpm.csv -> data/CIS_COMPO_bdpm.txt  (CIS -> active-substance denomination)
#      CIS_GENER_bdpm.csv -> data/CIS_GENER_bdpm.txt  (generic-group label per CIS)
#    Dataset page: https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante
BDPM_URL="https://www.data.gouv.fr/api/1/datasets/r/fecf69dd-ca9f-4902-95dd-4e0ec6ab92f0"

if [ ! -f data/CIS_RCP.csv ]; then
  echo "Downloading RCP dump..."
  wget -O data/CIS_RCP.zip "$RCP_URL"
  echo "Unzip data/CIS_RCP.zip so that data/CIS_RCP.csv exists, then re-run build."
fi

if [ ! -f data/CIS_bdpm.txt ] || [ ! -f data/CIS_COMPO_bdpm.txt ] || [ ! -f data/CIS_GENER_bdpm.txt ]; then
  echo "Downloading CIS_bdpm zip..."
  if wget -O data/CIS_bdpm.zip "$BDPM_URL"; then
    # The archive holds several *_bdpm.csv files; extract the three we use.
    # CIS_bdpm is required (titles + search); COMPO/GENER are optional joins for
    # scrape-rcp.py's frequency matching (it degrades to name-only if absent).
    unzip -p data/CIS_bdpm.zip CIS_bdpm.csv       > data/CIS_bdpm.txt
    unzip -p data/CIS_bdpm.zip CIS_COMPO_bdpm.csv > data/CIS_COMPO_bdpm.txt
    unzip -p data/CIS_bdpm.zip CIS_GENER_bdpm.csv > data/CIS_GENER_bdpm.txt
    rm -f data/CIS_bdpm.zip
  else
    echo "WARN: CIS_bdpm download failed; build will fall back to HTML denominations."
  fi
fi

echo "Done. Now run: uv run build.py"
