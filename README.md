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

- `dist/rcp/<cis>-<slug>.html` : une page nettoyée par médicament
- `dist/search-index.json` : consommé par la recherche côté client
- `style.css`, `search.js`, et un jumeau `.gz`/`.br` pour chaque fichier texte

Voir [CLAUDE.md](CLAUDE.md) pour l'architecture détaillée.

## Source des données

[Base de données publique des médicaments (BDPM)](https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante),
ANSM. Le dépôt le plus récent date du **2 mai 2022**, donc le contenu est ancien
et peut ne plus être exact. Ce site n'est affilié ni à l'ANSM ni à aucune
autorité et ne remplace pas un avis médical professionnel.

## Crédits

Réalisé avec l'aide de [Claude Code](https://claude.com/claude-code).
