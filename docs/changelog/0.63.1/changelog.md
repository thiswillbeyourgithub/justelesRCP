# 0.63.1 - 2026-10-17

## Bug fixes

- The status page no longer says the semantic search index is up to date when it does not know yet: it now shows when the indexing service keeps restarting, and which pages it had to set aside because they made it crash.
  fr: La page d'état n'affirme plus que l'index de la recherche sémantique est à jour quand elle ne le sait pas encore : elle signale désormais les redémarrages à répétition du service d'indexation, et les pages mises de côté parce qu'elles le faisaient planter.

## Improvements

- Pages waiting for indexing are now processed in random order, so one difficult page can no longer hold up all the others.
  fr: Les pages en attente d'indexation sont maintenant traitées dans un ordre aléatoire : une page difficile ne peut plus bloquer toutes les autres.
