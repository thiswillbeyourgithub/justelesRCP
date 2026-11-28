# 0.71.0 - 2026-11-28

## New features

- New medicines are found automatically: every day the official catalog is checked, and each new medicine is fetched first (from the EMA for a European authorization, otherwise from the ANSM) and becomes findable in search right away. [b7c17b7] [729058b]
  fr: Les nouveaux médicaments sont détectés automatiquement : chaque jour, le catalogue officiel est vérifié, et chaque nouveau médicament est récupéré en priorité (auprès de l'EMA pour une autorisation européenne, sinon de l'ANSM) puis devient aussitôt trouvable par la recherche. [b7c17b7] [729058b]
- European RCPs the EMA has revised are now spotted and refreshed promptly instead of waiting for their periodic check. [7a60d43] [b7c17b7]
  fr: Les RCP européens révisés par l'EMA sont désormais repérés et rafraîchis rapidement, sans attendre leur vérification périodique. [7a60d43] [b7c17b7]
- The status page has a new "Nouveaux médicaments" section: when the catalog was last checked, which medicines appeared or disappeared, their source (ANSM or EMA), whether they were fetched and indexed, and the EMA revisions. [6d1d3db]
  fr: La page d'état a une nouvelle section « Nouveaux médicaments » : date de la dernière vérification du catalogue, médicaments apparus ou retirés, leur source (ANSM ou EMA), s'ils ont été récupérés et indexés, et les révisions de l'EMA. [6d1d3db]

## Improvements

- A more legible font across the site (Atkinson Hyperlegible Next), served by the site itself. [c3b1fba]
  fr: Une police plus lisible sur tout le site (Atkinson Hyperlegible Next), servie par le site lui-même. [c3b1fba]

## Bug fixes

- The search within an RCP no longer jumps to a section heading instead of the matching passage on some pages. [060cc32]
  fr: La recherche dans un RCP ne renvoie plus vers un titre de section au lieu du passage correspondant sur certaines pages. [060cc32]
