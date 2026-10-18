# 0.63.2 - 2026-10-18

## Improvements

- When the way pages are cut up for the semantic search improves, every page stays searchable with the previous version while the index is rebuilt in the background, starting with the most prescribed drugs and the pages readers actually search.
  fr: Quand le découpage des pages pour la recherche sémantique s'améliore, toutes les pages restent consultables avec l'ancienne version pendant que l'index est reconstruit en arrière-plan, en commençant par les médicaments les plus prescrits et les pages réellement recherchées.

## Bug fixes

- Some leaflets presented whole paragraphs as section titles, which cluttered the table of contents, hid those paragraphs from the semantic search and could crash the indexing service. They are now shown as normal text.
  fr: Certains RCP présentaient des paragraphes entiers comme des titres de section : ils encombraient le sommaire, échappaient à la recherche sémantique et pouvaient faire planter le service d'indexation. Ils s'affichent désormais comme du texte normal.

- Restarting the indexing service during an update no longer wrongly sets aside the page it was working on.
  fr: Un redémarrage du service d'indexation lors d'une mise à jour ne met plus de côté à tort la page en cours de traitement.
