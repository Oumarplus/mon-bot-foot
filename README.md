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

## Étape 3 — Installer et lancer le bot

### Sur ton ordinateur (test rapide)

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="ton_token_ici"
export FOOTBALL_DATA_API_KEY="ta_cle_ici"

python bot.py
```

Le bot tourne tant que ton terminal est ouvert. Va sur Telegram, cherche ton
bot par son nom, envoie `/start`.

### Pour qu'il tourne 24/7 (accessible depuis ton téléphone à tout moment)

Le script doit rester actif en permanence sur un serveur — pas sur ton
téléphone directement. Options gratuites simples :

- **Render.com** : crée un "Background Worker", connecte ce dossier (via
  GitHub), ajoute les 2 variables d'environnement dans les Settings, déploie.
- **Railway.app** : même principe, très simple à connecter à un repo GitHub.
- **Un Raspberry Pi ou vieux PC chez toi**, avec le script lancé via `screen`
  ou un service systemd.

Une fois déployé, tu utilises le bot normalement depuis l'appli Telegram sur
ton téléphone — aucune app à installer, juste la conversation avec ton bot.

## Utilisation

- `/start` — présentation
- `/ligues` — liste des compétitions disponibles avec leurs codes
- `/check PL` — analyse la Premier League (remplace PL par PD, FL1, SA, BL1)

## Limites à connaître

- Le plan gratuit de football-data.org limite les requêtes/minute — pour
  une utilisation perso occasionnelle ça passe très bien.
- La détection se base uniquement sur le **nombre de buts**. Pour une analyse
  plus robuste, on pourrait ajouter les mouvements de cotes, les cartons,
  les tirs au but, etc. — dis-moi si tu veux que j'étende le bot dans ce sens.
