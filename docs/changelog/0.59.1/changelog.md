# 0.59.1 - 2026-10-03

## Bug fixes
- When updating a drug from the official source failed, the page could wrongly claim it had just been checked. It now keeps the date of the last real check. [e89f5da]
  fr: Quand la mise à jour d'un médicament depuis la source officielle échouait, la page pouvait annoncer à tort qu'elle venait d'être vérifiée. Elle garde maintenant la date de la dernière vérification réelle.
- In the search within a drug's RCP, changing the question while a search was still running could show the answers to the previous question. [946accd]
  fr: Dans la recherche à l'intérieur d'un RCP, modifier la question pendant une recherche en cours pouvait afficher les réponses à la question précédente.
- Dates could show one day early for readers in the French overseas departments of the Americas. [8853833]
  fr: Les dates pouvaient s'afficher avec un jour d'avance pour les lecteurs des départements d'outre-mer d'Amérique.
- When the refresh service is busy, the "Rafraîchir maintenant" button now says so instead of reporting it as unavailable. [b1b71f7]
  fr: Quand le service de mise à jour est occupé, le bouton « Rafraîchir maintenant » le dit au lieu d'annoncer qu'il est indisponible.
- Going back in the browser after a search now also restores the search box text. [b1b71f7]
  fr: Revenir en arrière dans le navigateur après une recherche remet aussi le bon texte dans la case de recherche.
- Withdrawn drugs kept as an archive can again be the target of links from other drug pages. [06ca9ba]
  fr: Les médicaments retirés conservés en archive peuvent de nouveau être la cible des liens depuis les autres fiches.

## Improvements
- Better protection of the site and its servers against abusive use. [72edb5b, 2d5b5f5, f37289d, b79f013, 843614c]
  fr: Meilleure protection du site et de ses serveurs contre les usages abusifs.
