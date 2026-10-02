# 0.59.0 - 2026-10-02

## New features
- The status page now has a "detail" button on the indexing card. It opens a table of every page still waiting to be indexed for the semantic search: why it is waiting, how many passages and tokens it holds, the last error and when it happened, plus a preview of the exact passages. [9d0dc18]
  fr: La page d'état propose un bouton « détail » sur la carte d'indexation. Il ouvre un tableau de chaque page en attente d'indexation pour la recherche sémantique : pourquoi elle attend, combien de passages et de tokens elle contient, la dernière erreur et sa date, avec un aperçu des passages exacts.

## Bug fixes
- A page whose search index was already up to date could stay counted as "waiting to be indexed" forever on the status page. It is now recognized as up to date. [a6644fe]
  fr: Une page dont l'index de recherche était déjà à jour pouvait rester comptée indéfiniment comme « en attente d'indexation » sur la page d'état. Elle est maintenant reconnue comme à jour.
