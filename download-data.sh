#!/usr/bin/env bash
# Fetch the source datasets into ./data (gitignored). Two public BDPM
# ("Base de données publique des médicaments") sources on the official sites.
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
#    It never changes, so it is fetched once and then skipped.
RCP_URL="https://www.data.gouv.fr/api/1/datasets/r/bdbe2367-1898-4848-ac85-6fe58a1bdf68"

if [ ! -f data/CIS_RCP.csv ]; then
  echo "Downloading RCP dump..."
  wget -O data/CIS_RCP.zip "$RCP_URL"
  echo "Unzip data/CIS_RCP.zip so that data/CIS_RCP.csv exists, then re-run build."
fi

# 2. CIS metadata: the CIS -> drug name mapping (official denominations, for
#    titles + search), plus two optional joins scrape-rcp.py uses to match drugs
#    to the frequency list via their active substance / reference brand (never to
#    build pages). These come from the LIVE daily BDPM download ("base officielle"),
#    NOT the frozen 2022 défi export above: the whole point is a CURRENT catalog,
#    so a drug authorized after 2022 (e.g. XURTA, CIS 68136124, AMM 2025) is in the
#    name map and therefore searchable and crawlable. Only names + joins are
#    refreshed this way; the RCP *text* is still not published in bulk (the frozen
#    dump above is the only source), so scrape-rcp.py fetches each new drug's RCP
#    on top. Each file is a plain tab-separated latin-1 .txt served directly (no zip):
#      CIS_bdpm.txt        Code_CIS <TAB> Denomination <TAB> ...   (required: titles + search)
#      CIS_COMPO_bdpm.txt  CIS -> active-substance denomination    (optional join)
#      CIS_GENER_bdpm.txt  generic-group label per CIS             (optional join)
#    They are small and refreshed daily upstream, so re-fetch them EVERY run
#    (unlike the ~1GB frozen RCP dump). Download to a temp file and swap on success
#    so a failed/partial download never clobbers a good existing copy.
#    Dataset: https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-base-officielle
BDPM_LIVE="https://base-donnees-publique.medicaments.gouv.fr/download/file"

for f in CIS_bdpm CIS_COMPO_bdpm CIS_GENER_bdpm; do
  echo "Downloading $f.txt (live BDPM)..."
  if wget -O "data/$f.txt.tmp" "$BDPM_LIVE/$f.txt" && [ -s "data/$f.txt.tmp" ]; then
    mv -f "data/$f.txt.tmp" "data/$f.txt"
  else
    rm -f "data/$f.txt.tmp"
    if [ -f "data/$f.txt" ]; then
      echo "WARN: $f.txt download failed; keeping the existing copy."
    elif [ "$f" = CIS_bdpm ]; then
      echo "WARN: $f.txt download failed and none present; build will fall back to HTML denominations."
    else
      echo "WARN: $f.txt download failed and none present; scrape-rcp.py frequency join degrades to name-only."
    fi
  fi
done

echo "Done. Now run: uv run build.py"
