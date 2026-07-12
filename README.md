<!-- Version française. English version: README.en.md
     IMPORTANT : README.md (FR) et README.en.md (EN) doivent rester synchronisés.
     Quand vous modifiez l'un, mettez l'autre à jour en conséquence. -->

# justelesRCP

*Read this in [English](README.en.md).*

**Juste les résumés des caractéristiques du produit.** Un site statique rapide,
sans pub et sans compte, qui sert les RCP (résumés des caractéristiques du
produit) des médicaments vendus en France, à partir du jeu de données public
ANSM / BDPM. Conçu comme une alternative légère aux sites de médicaments lents et
à but lucratif.

- Aucun serveur applicatif, aucune base de données : chaque page est un fichier
  statique précalculé.
- Recherche instantanée côté client sur ~15 600 médicaments, plus des pages de
  navigation A-Z indexables par les moteurs de recherche.
- Précompressé (brotli + gzip), servi par un conteneur Caddy durci en lecture
  seule.
- Mesure d'audience respectueuse de la vie privée (umami : sans cookies, sans
  traçage publicitaire). Site hébergé en France.

> [!CAUTION]
> **Prototype en développement précoce.** Ce site est à un stade précoce :
> certaines fonctionnalités manquent, des bugs sont probables et les données
> peuvent être incomplètes ou obsolètes. Ne vous en servez pas comme source
> unique pour une décision médicale ; vérifiez toujours une source à jour et
> faisant autorité, et demandez conseil à un professionnel de santé.

> [!WARNING]
> **Le contenu des RCP n'est pas à jour.** Le dernier dépôt du jeu de données sur
> data.gouv.fr date du **2 mai 2022**, donc les pages reflètent des informations
> médicaments potentiellement obsolètes. Vérifiez toujours une source faisant
> autorité et à jour avant de vous fier à une information présente ici.

## Démarrage rapide

```bash
./download-data.sh                                  # récupère les jeux de données source dans ./data (gitignored)
uv run build.py                                     # génère ./dist à partir de ./data
cp docker/env.example docker/.env                   # optionnel : configurer les statistiques
docker compose -f docker/docker-compose.yml up -d   # sert sur http://localhost:8459
```

Placez votre propre reverse proxy TLS devant le port 8459.

La configuration optionnelle au runtime (statistiques [umami](https://umami.is)
respectueuses de la vie privée et bannière « travaux en cours ») se trouve dans
`docker/.env` ; voir `docker/env.example`. Laissez le fichier vide pour zéro
traçage. Rien n'est chargé depuis un CDN ; une CSP stricte n'ouvre que votre
propre origine umami quand vous définissez `ANALYTICS_URL`.

## Comment ça marche

`build.py` lit le dump des RCP de l'ANSM (`data/CIS_RCP.csv` :
`Code_CIS <TAB> RCP_html`), nettoie et restyle chaque document, puis écrit :

- `dist/rcp/<cis>-<slug>.html` : une page nettoyée par médicament, avec une table
  des matières latérale (« Sommaire ») pour naviguer entre les rubriques
- `dist/search-index.json` : consommé par la recherche côté client
- `dist/a-propos.html` : la page « À propos »
- `style.css`, `search.js`, et un jumeau `.gz`/`.br` pour chaque fichier texte

La génération est **incrémentale** : un cache par médicament
(`dist/.build-manifest.json`) évite de re-parser et re-compresser les documents
inchangés, ce qui accélère fortement les redéploiements après un simple
rafraîchissement des données. Le numéro de version n'est pas figé dans le HTML
des pages (il est servi au runtime via `app-version.js`), de sorte qu'un simple
changement de version n'invalide pas le cache.

Voir [CLAUDE.md](CLAUDE.md) pour l'architecture détaillée.

## Garder les RCP à jour

Le dump `CIS_RCP.csv` est figé (2 mai 2022) et c'est le seul export *HTML* en
masse qui existe : le téléchargement officiel de la BDPM, lui, est rafraîchi
quotidiennement mais ne contient que des métadonnées, jamais le corps HTML des
RCP. Pour rafraîchir les RCP sans renoncer à l'architecture statique, `scrape-rcp.py`
récupère en arrière-plan les pages de médicaments sur le site de l'ANSM et écrit
un fichier de surcharge par médicament (`data/rcp/<cis>.html`) que `build.py`
préfère au dump de 2022. Rien de dynamique ne tourne au moment de servir les pages.

Chaque page RCP indique depuis quand ses informations datent (« Informations à
jour au … »), calculé à partir de la date de récupération (ou du 2 mai 2022 pour
le socle), avec un avertissement affiché quand les données ont plus d'un an.

```bash
uv run scrape-rcp.py --limit 60   # rafraîchit 60 médicaments (les plus consultés d'abord)
uv run build.py                    # régénère (incrémental : seuls les changements)
```

Un CIS rafraîchi depuis moins de `--ttl-days` (30 par défaut) est ignoré. L'ordre
de priorité vient d'une liste de fréquence JSONL (`--frequency`, par défaut
`data/drugs_frequency.jsonl`) où chaque ligne est
`{"term": "<nom de médicament ou substance>", "score": <plus haut = plus tôt>}` ;
les termes sont rapprochés du nom de chaque médicament (insensible aux accents),
et un médicament qu'aucun terme ne matche reçoit le score du 25e centile pour
rester à une priorité moyenne. Idéal en tâche `cron`. Voir l'en-tête du script
pour toutes les options (`--all` pour un scan complet unique, `--only` pour des
CIS précis).

## Source des données et licence

Les données proviennent de la
[base de données publique des médicaments (BDPM)](https://base-donnees-publique.medicaments.gouv.fr/telechargement),
publiée par l'ANSM. Ce sont des **données ouvertes** sous
[Licence Ouverte / Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/),
et justelesRCP les réutilise **dans le respect de cette licence** : citation de la
source et de sa date, aucune altération du sens, aucune suggestion de caractère
officiel. Le socle des RCP date du **2 mai 2022** (dépôt en masse le plus récent),
donc le contenu peut être ancien et ne plus être exact ; certaines pages sont
progressivement rafraîchies. Cette réutilisation ne confère aucun caractère officiel
et ne suggère aucune reconnaissance de l'ANSM, de la HAS ou de l'UNCAM. Ce site
n'est affilié à aucune autorité et ne remplace pas un avis médical professionnel.

## Crédits

Réalisé avec l'aide de [Claude Code](https://claude.com/claude-code).
