# 0.46.0 - 2026-07-21

## New features
- Medicines withdrawn from the French catalogue are no longer dead ends: their page is kept as a clearly labelled archive copy, and search marks them [RETIRE]. [2c5308a]
  fr: Les médicaments retirés du catalogue français ne mènent plus à une impasse : leur page est conservée comme copie d'archive clairement signalée, et la recherche les marque « [RETIRÉ] ».

## Bug fixes
- The "Rafraîchir maintenant" button now tells you honestly what happened, and no longer reloads the page in a loop for a withdrawn medicine. [0398ed7]
  fr: Le bouton « Rafraîchir maintenant » indique désormais honnêtement ce qui s'est passé, et ne recharge plus la page en boucle pour un médicament retiré.
- A reader closing a page mid-request no longer leaves errors behind on the server. [038aa8d]
  fr: Fermer une page en cours de chargement ne laisse plus d'erreurs derrière soi côté serveur.
