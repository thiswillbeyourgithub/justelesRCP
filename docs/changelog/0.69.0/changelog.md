# 0.69.0 - 2026-10-30

## Bug fixes

- In the semantic search, clicking a result now takes you to the exact passage it came from. Until now the site looked for the passage by its text, which missed whenever that text was split across the page (table cells, a short line skipped in the middle): about one result in five then opened at the top of its section. Each result now remembers where it sits on the page. [835184e, 280a0bd, 88675fd, e4fc5da]
  fr: Dans la recherche sémantique, cliquer un résultat mène désormais au passage exact dont il provient. Jusqu'ici le site cherchait le passage par son texte, ce qui échouait dès que ce texte était éclaté sur la page (cellules de tableau, courte ligne sautée au milieu) : environ un résultat sur cinq s'ouvrait alors au début de sa rubrique. Chaque résultat retient maintenant sa position sur la page. [835184e, 280a0bd, 88675fd, e4fc5da]
- On 7 European-authorization pages whose sections the EMA document left unnumbered, semantic-search results led nowhere. They now open the right section. [e4fc5da]
  fr: Sur 7 pages d'autorisation européenne dont le document de l'EMA ne numérotait pas les rubriques, les résultats de la recherche sémantique ne menaient nulle part. Ils ouvrent désormais la bonne rubrique. [e4fc5da]
