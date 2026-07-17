<!-- Version française. English version: README.en.md
     IMPORTANT : README.md (FR) et README.en.md (EN) doivent rester synchronisés.
     Quand vous modifiez l'un, mettez l'autre à jour en conséquence. -->

# justelesRCP

### 🌐 Site en ligne : **[justelesrcp.olicorne.org](https://justelesrcp.olicorne.org)**

*Read this in [English](README.en.md).*

**Juste les résumés des caractéristiques du produit.** Un site statique rapide,
sans pub et sans compte, qui donne accès aux RCP (résumés des caractéristiques du
produit) des médicaments vendus en France. L'objectif : aider soignants et
patients à trouver plus vite une information fiable et officielle, sans publicité
et sans sacrifier leur vie privée. Une alternative légère aux sites de médicaments
lents et à but lucratif.

- Chaque page est un fichier statique précalculé : aucun serveur applicatif,
  aucune base de données, rien à traquer côté visiteur.
- Recherche instantanée sur ~15 600 médicaments, plus des pages de navigation
  A-Z pensées pour les moteurs de recherche (plan du site, liens canoniques,
  données structurées et fil d'Ariane sur chaque page).
- **Recherche en langage naturel à l'intérieur d'un RCP** : posez une question
  telle que « puis-je le prendre enceinte ? » et la page met en avant les
  passages qui répondent au *sens* de votre question, pas seulement aux mots
  exacts. Votre question est analysée sur notre propre serveur puis aussitôt
  oubliée : jamais enregistrée, jamais transmise à un tiers.
- Liens croisés entre médicaments : chaque page relie automatiquement les noms de
  médicaments et de substances qu'elle cite vers leurs propres pages (jamais un
  lien mort). Ces liens sont ajoutés par justelesRCP et ne font pas partie du
  texte officiel.
- Médicaments à autorisation européenne (dont le RCP est publié par l'EMA et non
  par l'ANSM, ex. Abilify) : leur RCP officiel est récupéré, converti et affiché
  ici, avec un lien vers le document source de l'EMA.
- Mesure d'audience respectueuse de la vie privée (umami : sans cookies, sans
  traçage publicitaire). Site hébergé en France.

> [!CAUTION]
> **Prototype en développement.** Certaines fonctionnalités manquent et des
> bugs sont probables.

## Comment ça marche

Le point de départ est le
[dernier export en masse des RCP de l'ANSM](https://www.data.gouv.fr/datasets/base-de-donnees-publique-des-medicaments-defi-idoc-sante),
**figé au 2 mai 2022** : c'est le seul export HTML complet qui existe (le téléchargement
officiel quotidien de la BDPM ne contient que des métadonnées, pas le texte des
RCP). Ce socle ne couvre que l'ANSM, pas les médicaments à autorisation
européenne.

À partir de là, le contenu est rafraîchi **progressivement** : un service de fond
récupère les pages officielles quelques-unes à la fois, toutes les quelques
minutes, en commençant par les médicaments les plus consultés, et n'importe quel
lecteur peut demander le rafraîchissement immédiat d'une page via un bouton. Au
fil du temps on aboutit à une **copie locale à jour** des données, sans jamais
solliciter agressivement les sites sources : requêtes peu fréquentes, limitées en
débit, et une seule requête même si plusieurs lecteurs consultent la même page.

Pour les médicaments à autorisation européenne, le RCP est publié par l'EMA sous
forme de PDF. J'ai posé la question à l'EMA : ils m'ont confirmé qu'il fallait le
récupérer nous-mêmes ainsi, faute d'accès prévu aux RCP. justelesRCP télécharge
donc ce PDF officiel, le convertit en page lisible et renvoie toujours vers le
document source.

Tout le rendu est précalculé : au moment de servir les pages, rien de dynamique ne
tourne. La recherche en langage naturel et le rafraîchissement à la demande sont
deux petits services compagnons optionnels et durcis, séparés du serveur web.

Pour l'exécuter vous-même ou pour l'architecture détaillée, voir
[CLAUDE.md](CLAUDE.md).

## Source des données et licence

Les données proviennent de la
[base de données publique des médicaments (BDPM)](https://base-donnees-publique.medicaments.gouv.fr/telechargement),
publiée par l'ANSM, réutilisées sous
[Licence Ouverte / Etalab 2.0](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)
dans le respect de cette licence : citation de la source et de sa date, aucune
altération du sens, aucune suggestion de caractère officiel. Le socle des RCP date
du 2 mai 2022, le contenu peut donc être ancien même si les pages sont
progressivement rafraîchies. justelesRCP n'est affilié à aucune autorité (ANSM,
HAS, EMA…) et ne remplace pas un avis médical professionnel.

## Des questions ?

Le plus simple est de lire le code, d'ouvrir une *issue*, ou de me contacter via
[olicorne.org/fr/contact](https://olicorne.org/fr/contact).

## Crédits

Réalisé avec l'aide de [Claude Code](https://claude.com/claude-code).
