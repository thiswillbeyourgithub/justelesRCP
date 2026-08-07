# 0.55.8 - 2026-08-07

## Bug fixes
- The site's status page showed a permanent indexing delay of a few hundred pages that never went away. Those pages are medicines withdrawn from the market, whose text is only an archive copy from 2022: they are never indexed for the search inside a leaflet, so they are no longer counted as waiting.
  fr: La page d'état du site affichait un retard d'indexation permanent de quelques centaines de pages qui ne disparaissait jamais. Ces pages sont des médicaments retirés du marché, dont le texte n'est qu'une copie d'archive de 2022 : ils ne sont jamais indexés pour la recherche dans la notice, ils ne sont donc plus comptés comme en attente.
- On the page of a withdrawn medicine, the semantic search box waited about a minute before giving up. It now says right away that this archive page is not indexed.
  fr: Sur la page d'un médicament retiré, la recherche sémantique attendait environ une minute avant d'abandonner. Elle indique désormais tout de suite que cette page d'archive n'est pas indexée.

## Improvements
- The background indexing check is much faster (a few seconds instead of several minutes), so newly refreshed pages become searchable sooner and the server stays free for readers' searches.
  fr: La vérification d'indexation en arrière-plan est bien plus rapide (quelques secondes au lieu de plusieurs minutes), donc les pages fraîchement mises à jour deviennent consultables plus tôt et le serveur reste disponible pour les recherches des lecteurs.
