<!-- Version française. English version: README.en.md
     IMPORTANT : README.md (FR) et README.en.md (EN) doivent rester synchronisés.
     Quand vous modifiez l'un, mettez l'autre à jour en conséquence. -->

# justelesRCP

### 🌐 Site en ligne : **[justelesrcp.olicorne.org](https://justelesrcp.olicorne.org)**

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
- *(Prévu)* Recherche sémantique par embeddings à l'intérieur d'une page RCP
  donnée, calculée entièrement dans le navigateur (côté client), donc
  respectueuse de la vie privée : aucune requête n'est envoyée à un serveur.
- Liens croisés entre médicaments : chaque page RCP relie automatiquement les
  noms de médicaments et de substances qu'elle cite (par ex. « oméprazole »,
  « carbamazépine ») vers leurs propres pages, avec un encart « Médicaments
  liés » en bas. Seules les substances qui ont une page RCP dans le jeu de
  données sont liées (jamais un lien mort). Ces liens sont ajoutés par
  justelesRCP et ne font pas partie du texte officiel de l'ANSM.
- Précompressé (brotli + gzip), servi par un conteneur Caddy durci en lecture
  seule.
- Mesure d'audience respectueuse de la vie privée (umami : sans cookies, sans
  traçage publicitaire). Site hébergé en France.

> [!CAUTION]
> **Prototype en développement.** Certaines fonctionnalités manquent et des
> bugs sont probables.

## Comment ça marche

`build.py` lit le dump des RCP de l'ANSM (`data/CIS_RCP.csv` :
`Code_CIS <TAB> RCP_html`), nettoie et restyle chaque document, puis écrit :

- `dist/rcp/<cis>-<slug>.html` : une page nettoyée par médicament, avec une table
  des matières latérale (« Sommaire ») pour naviguer entre les rubriques
- `dist/eu/<cis>-<slug>.html` : pour les médicaments à AMM européenne centralisée
  (dont le RCP est publié par l'EMA et non par l'ANSM, ex. Abilify), une page
  d'aiguillage qui renvoie vers le RCP officiel sur le site de l'EMA et vers les
  génériques équivalents présents ici. Ils restent ainsi trouvables via la
  recherche
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

## Garder les RCP à jour

Le dump `CIS_RCP.csv` est figé (2 mai 2022) et c'est le seul export *HTML* en
masse qui existe : le téléchargement officiel de la BDPM, lui, est rafraîchi
quotidiennement mais ne contient que des métadonnées, jamais le corps HTML des
RCP. Pour rafraîchir les RCP sans renoncer à l'architecture statique, `scrape-rcp.py`
récupère en arrière-plan les pages de médicaments sur le site de l'ANSM et écrit
un fichier de surcharge par médicament (`data/rcp/<cis>.html.gz`, gzip par défaut)
que `build.py` préfère au dump de 2022. Rien de dynamique ne tourne au moment de
servir les pages.

Chaque page RCP affiche en tête la date de révision du RCP par l'ANSM
(« Informations à jour au … », lue dans le corps du RCP), plus une petite ligne
« Version vérifiée par justelesRCP le … » indiquant quand nous avons contrôlé
cette copie auprès de l'ANSM pour la dernière fois. Un avertissement n'apparaît
que si *notre copie* n'a pas été recontrôlée depuis plus d'un an, et non parce
que le texte de l'ANSM est ancien : un RCP révisé en 2021 mais jamais modifié
reste le texte officiel en vigueur, son ancienneté seule n'est pas de
l'obsolescence.

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

Deux réglages se pilotent aussi par variable d'environnement, pratique pour un
`cron` : `RCP_OVERLAY_GZIP` (surcharges gzippées, activé par défaut ; `--no-gzip`
pour du HTML brut, `build.py` lit les deux de façon transparente) et
`RCP_SCRAPE_RATE_SECONDS` (délai de base entre deux requêtes, un aléa est ajouté).
Le script journalise une barre de progression avec estimation du temps restant et
indique à chaque page si elle est demandée manuellement (`--only`, « user ») ou
issue de la file automatique (« timer »).

### Rafraîchir un RCP à la demande (service optionnel)

Chaque page RCP possède un bouton « Rafraîchir maintenant », et une page dont
notre copie a été récupérée il y a plus d'un an se rafraîchit automatiquement au
chargement. Les deux
appellent un petit service compagnon (`refresh-service.py`, `POST
/api/refresh/<cis>`) qui récupère la page ANSM en direct, écrit la surcharge et
régénère cette seule page. C'est **la seule partie dynamique** du projet : elle
tourne dans un conteneur séparé et durci (en lecture seule sauf trois chemins
précis) pour que le serveur web, lui, reste entièrement en lecture seule, et des
limiteurs de débit par voie plus un plancher par médicament (plusieurs visiteurs sur
la même page ne déclenchent qu'une seule requête) évitent de solliciter le site de
l'ANSM. Tout est **facultatif** : `docker compose ... up` démarre le service, mais
`docker compose ... up web` le laisse de côté ; sans lui, `/api/*` renvoie une
erreur et le bouton signale simplement l'indisponibilité, le site restant 100 %
statique. En arrière-plan, un **crawler** parcourt en continu toutes les pages par
ordre de fréquence (unités vendues) et rafraîchit celles dont la copie dépasse le
seuil d'ancienneté (`REFRESH_CRAWL_TTL_DAYS`, 12 mois par défaut), puis se met en
veille jusqu'à ce que la plus ancienne repasse ce seuil. Le crawler et les clics
« Rafraîchir maintenant » tournent sur **deux voies distinctes, chacune avec son
propre limiteur de débit** : un clic est donc récupéré quasi immédiatement sur sa
voie rapide (`REFRESH_DEMAND_RATE_SECONDS`, ~5 s) au lieu d'attendre derrière le
filet lent du crawler (`REFRESH_RATE_SECONDS`, ~2 min) ; les deux voies restent
séquentielles et douces. Il tient des statistiques de crawl par
origine (bouton / automatique / crawler) consultables via `GET /api/stats`. Les
réglages (`REFRESH_*`) sont dans `docker/.env` ; voir `docker/env.example`.

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

Pour maintenir les RCP à jour, justelesRCP récupère ponctuellement certaines pages
directement sur le site public de l'ANSM. Ces requêtes sont peu fréquentes, limitées
en débit et accompagnées d'une adresse de contact, afin de ne pas peser sur ce
service public ; aucune donnée personnelle des visiteurs n'est transmise à cette
occasion.

## Crédits

Réalisé avec l'aide de [Claude Code](https://claude.com/claude-code).
