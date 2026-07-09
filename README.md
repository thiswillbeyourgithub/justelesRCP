# justelesRCP

**Juste les résumés des caractéristiques du produit.** A fast, ad-free, no-account
static site for the RCP (résumés des caractéristiques du produit) of medicines
sold in France, from the ANSM / BDPM public dataset. Built as a lightweight
alternative to slow, for-profit medicine sites.

- No application server, no database: every page is precomputed to a static file.
- Client-side instant search over ~15,600 medicines, plus crawlable A-Z browse pages.
- Precompressed (brotli + gzip) and served by a hardened, read-only Caddy container.

## Quick start

```bash
./download-data.sh        # fetch the source datasets into ./data (gitignored)
uv run build.py           # render ./dist from ./data
docker compose up -d      # serve on http://localhost:8080
```

Put your own TLS reverse proxy in front of port 8080.

## How it works

`build.py` reads the ANSM RCP dump (`data/CIS_RCP.csv`: `Code_CIS <TAB> RCP_html`),
cleans and restyles each document, and writes:

- `dist/rcp/<cis>-<slug>.html` : one page per medicine
- `dist/search-index.json` : the name index for client-side search
- homepage + `style.css` + `search.js`, all with `.gz`/`.br` siblings

See [CLAUDE.md](CLAUDE.md) for the full architecture and gotchas.

## Data source & disclaimer

Data: [Base de données publique des médicaments (BDPM)](https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante),
published by the ANSM. This site is not affiliated with the ANSM or any authority
and does not replace professional medical advice.

## Credits

Built with the help of [Claude Code](https://claude.com/claude-code).
