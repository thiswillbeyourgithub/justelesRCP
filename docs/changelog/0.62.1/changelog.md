# 0.62.1 - 2026-10-17

## Bug fixes

- The semantic search index had stopped updating: one page with very long tables made the indexing service run out of memory and restart in a loop. Long passages are now processed in smaller groups, so the recently updated pages are indexed again.
  fr: L'index de la recherche sémantique ne se mettait plus à jour : une page aux tableaux très longs faisait manquer de mémoire le service d'indexation, qui redémarrait en boucle. Les passages longs sont maintenant traités par petits groupes, et les pages récemment mises à jour sont de nouveau indexées.
