# Bot Telegram — Détection d'anomalies de buts

Ce bot analyse les matchs de football terminés récemment (PL, Liga, Ligue 1,
Serie A, Bundesliga) et repère ceux dont le nombre total de buts s'écarte
fortement de la moyenne habituelle des deux équipes.

**⚠️ À lire absolument** : le bot détecte des écarts *statistiques*, pas des
matchs truqués. Un score élevé peut venir d'une tactique agressive, de
cartons rouges, d'un changement d'entraîneur, etc. C'est un outil d'alerte à
recouper avec d'autres sources, pas une accusation.

## Étape 1 — Créer ton bot Telegram (2 min)

1. Ouvre Telegram, cherche **@BotFather**
2. Envoie `/newbot`, choisis un nom et un identifiant (doit finir par "bot")
3. BotFather te donne un **token** du type `123456:ABC-DEF...` → garde-le

## Étape 2 — Récupérer une clé API football (gratuite)

1. Va sur https://www.football-data.org/client/register
2. Crée un compte gratuit → tu reçois une clé API par email
3. Le plan gratuit couvre : Premier League, Liga, Ligue 1, Serie A, Bundesliga
   (10 requêtes/minute, largement suffisant pour cet usage)

## Étape 2bis — (Optionnel) Clé API-Football pour les blessures/suspensions

Sans cette clé, `/predict` fonctionne normalement mais sans la section
"Absences signalées".

1. Va sur https://www.api-football.com/ (ou via RapidAPI) et crée un compte
   gratuit
2. Récupère ta clé API (100 requêtes/jour sur le plan gratuit)
3. Cette clé sera à ajouter comme 3e variable d'environnement : `API_FOOTBALL_KEY`

## Étape 2ter — (Optionnel) Clé The Odds API pour le suivi des cotes

Sans cette clé, `/odds` répond simplement qu'elle n'est pas configurée — le
reste du bot fonctionne normalement.

1. Va sur https://the-odds-api.com/ et crée un compte gratuit
2. Récupère ta clé API (500 requêtes/mois sur le plan gratuit)
3. Cette clé sera à ajouter comme 4e variable d'environnement : `ODDS_API_KEY`

⚠️ Avec seulement 500 requêtes/mois, `/odds` est pensé pour un usage
**manuel et occasionnel** — évite de le taper en boucle pour ne pas
épuiser le quota avant la fin du mois.

## Étape 3 — Installer et lancer le bot

### Sur ton ordinateur (test rapide)

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="ton_token_ici"
export FOOTBALL_DATA_API_KEY="ta_cle_ici"
export API_FOOTBALL_KEY="ta_cle_optionnelle_ici"   # optionnel, pour les absences
export ODDS_API_KEY="ta_cle_optionnelle_ici"       # optionnel, pour le suivi des cotes

python bot.py
```

Le bot tourne tant que ton terminal est ouvert. Va sur Telegram, cherche ton
bot par son nom, envoie `/start`.

### Pour qu'il tourne 24/7 (accessible depuis ton téléphone à tout moment)

Le script doit rester actif en permanence sur un serveur — pas sur ton
téléphone directement. Options gratuites simples :

- **Render.com** : crée un "Background Worker", connecte ce dossier (via
  GitHub), ajoute les variables d'environnement dans les Settings, déploie.
- **Railway.app** : même principe, très simple à connecter à un repo GitHub.
- **Un Raspberry Pi ou vieux PC chez toi**, avec le script lancé via `screen`
  ou un service systemd.

Une fois déployé, tu utilises le bot normalement depuis l'appli Telegram sur
ton téléphone — aucune app à installer, juste la conversation avec ton bot.

## Utilisation

- `/start` — présentation
- `/ligues` — liste des compétitions disponibles avec leurs codes
- `/check PL` — analyse la Premier League (remplace PL par PD, FL1, SA, BL1)
- `/predict PL` — pronostics sur les matchs à venir (7 prochains jours) : Plus/Moins 1.5/2.5/3.5 buts, les 2 équipes marquent, victoire domicile/nul/extérieur, et les 3 scores exacts les plus probables
- `/subscribe` — reçois une alerte automatique chaque jour (~8h UTC) s'il y a des matchs suspects sur les 8 compétitions
- `/unsubscribe` — stoppe l'alerte quotidienne
- `/live PL` — vérifie tout de suite les matchs en direct sur cette compétition
- `/live_subscribe` — active la surveillance automatique des matchs en cours (toutes les 10 min), pour repérer un sursaut de buts suspect pendant qu'un match se joue
- `/live_unsubscribe` — stoppe cette surveillance
- `/odds PL` — vérifie les cotes actuelles (1X2) et leur mouvement depuis ta dernière vérification (nécessite `ODDS_API_KEY`, voir Étape 2ter)

## Suivi des mouvements de cotes — comment ça marche et ses limites

`/odds` récupère, pour chaque match à venir d'un championnat, les cotes
moyennes de plusieurs bookmakers (converties en probabilités implicites
1X2). À chaque nouvelle vérification, il compare avec la précédente et
signale un match si une probabilité a bougé de 8 points ou plus.

Un mouvement de cote important avant un match peut parfois signaler que le
marché a intégré une information (blessure de dernière minute, enjeu du
match...) — ou simplement une correction normale du marché. Ce n'est pas
non plus une preuve de quoi que ce soit.

⚠️ **Limites importantes** :
- Plan gratuit = 500 requêtes/mois **au total** → à utiliser à la main,
  ponctuellement, pas en boucle automatique
- La première vérification sur un match ne peut jamais montrer de
  mouvement (pas de point de comparaison encore) — normal
- Toutes les compétitions du bot ne sont pas forcément couvertes par
  The Odds API (dépend des championnats suivis par leurs bookmakers partenaires)

## Surveillance en direct — comment ça marche et ses limites

Le bot vérifie périodiquement les matchs en cours et retient le score à
chaque passage. S'il détecte plusieurs buts marqués d'un coup sur une courte
fenêtre (par défaut : 2 buts en 15 minutes), il envoie une alerte.

⚠️ **Limites importantes à connaître** :
- Le plan gratuit de football-data.org ne garantit pas un flux temps réel —
  les scores peuvent être retardés de quelques minutes
- La détection dépend de la fréquence de passage du bot (10 min par défaut) :
  un sursaut qui se produit puis se stabilise entre deux vérifications peut
  passer inaperçu
- Le premier passage sur un match ne peut jamais générer d'alerte (il n'y a
  pas encore de score précédent à comparer) — c'est normal

## Comment fonctionne /predict

Le pronostic utilise un **modèle de Poisson**, la méthode statistique standard
pour ce type d'estimation :

1. Pour chaque équipe, on calcule sa moyenne de buts marqués/encaissés sur ses
   derniers matchs **dans le même contexte** (à domicile pour l'équipe qui
   reçoit, à l'extérieur pour l'équipe qui se déplace)
2. On en déduit un nombre de buts "attendu" pour chaque équipe
3. On simule toutes les combinaisons de scores possibles pour en tirer les
   probabilités (plus/moins, BTTS, 1X2, scores exacts, groupes de scores)

En plus, `/predict` affiche pour chaque match :

- **Élo** : la cote de force de chaque équipe (via clubelo.com, API publique
  gratuite). Un écart important entre les deux Élo indique un favori net.
- **Forme récente** : résultats (V/N/D) des 5 derniers matchs de chaque
  équipe dans le même contexte domicile/extérieur
- **Absences signalées** : joueurs blessés/suspendus (nécessite la clé
  `API_FOOTBALL_KEY`, voir Étape 2bis — sinon cette ligne n'apparaît pas)

⚠️ C'est une estimation statistique basée sur la forme récente, pas une
certitude — les compositions d'équipe, l'enjeu du match, etc. ne sont
qu'en partie pris en compte.

## Les 3 signaux d'analyse

Le bot combine maintenant trois indicateurs statistiques indépendants. Un
match peut déclencher un, deux ou les trois signaux :

1. **Nombre de buts** — écart (z-score) entre le total de buts du match et
   la moyenne récente des deux équipes
2. **Mi-temps** — buts anormalement concentrés en 2e période (75%+ des buts
   après la pause, avec au moins 3 buts au total)
3. **Face-à-face** — écart par rapport à l'historique des confrontations
   directes entre ces deux équipes précises (nécessite au moins 3
   confrontations passées connues de l'API)

⚠️ Note sur `/subscribe` : la liste des abonnés est stockée dans un fichier
local sur le serveur. Sur certains hébergeurs (comme Railway avec un disque
éphémère), cette liste peut être réinitialisée après un redéploiement — il
suffira de retaper `/subscribe`.

## Limites à connaître

- Le plan gratuit de football-data.org limite les requêtes/minute — pour
  une utilisation perso occasionnelle ça passe très bien.
- La détection se base uniquement sur le **nombre de buts**. Pour une analyse
  plus robuste, on pourrait ajouter les mouvements de cotes, les cartons,
  les tirs au but, etc. — dis-moi si tu veux que j'étende le bot dans ce sens.
