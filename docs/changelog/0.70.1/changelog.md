# 0.70.1 - 2026-11-10

## Bug fixes

- European-authorization pages awaiting the corrected conversion from 0.70.0 are now redone faster: when the Internet Archive holds the very same version of the official PDF, it is used instead of waiting for the EMA's site, which refuses most of our downloads. [8af35d0]
  fr: Les pages d'autorisation européenne en attente de la conversion corrigée de la 0.70.0 sont désormais refaites plus vite : quand l'Internet Archive conserve exactement la même version du PDF officiel, elle est utilisée au lieu d'attendre le site de l'EMA, qui refuse la plupart de nos téléchargements. [8af35d0]
- The search within a medicine's RCP stayed unavailable on a few pages whose long dosing tables made the server run out of memory while preparing them. They are now prepared in smaller pieces and get their search back. [682d9fb] [12e4b79] [eebc981]
  fr: La recherche dans le RCP d'un médicament restait indisponible sur quelques pages dont les longs tableaux de posologie faisaient manquer de mémoire au serveur pendant leur préparation. Elles sont désormais préparées par plus petits morceaux et retrouvent leur recherche. [682d9fb] [12e4b79] [eebc981]
