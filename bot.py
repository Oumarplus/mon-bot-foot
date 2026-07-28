"""
Bot Telegram - Détection de matchs de football avec un nombre de buts anormal
================================================================================

Ce bot compare le nombre de buts de chaque match terminé récemment à la
moyenne historique des deux équipes concernées, et calcule un score
statistique (z-score) pour repérer les matchs "hors norme".

⚠️ IMPORTANT : ceci est un outil d'analyse statistique, PAS une preuve de
match truqué. Un score élevé peut avoir plein d'explications légitimes
(tactique, forme du moment, cartons rouges, météo...). À utiliser comme
signal d'alerte à recouper, jamais comme accusation.

Installation : voir README.md
"""

import os
import logging
import statistics
from datetime import datetime, timedelta

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Configuration (lues depuis les variables d'environnement) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY")
FOOTBALL_API_BASE = "https://api.football-data.org/v4"

# Compétitions supportées par le plan gratuit de football-data.org
COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "FL1": "Ligue 1",
    "SA": "Serie A",
    "BL1": "Bundesliga",
}

# Seuil : au-delà de ce z-score (écart-type par rapport à la moyenne
# habituelle des deux équipes), un match est considéré "suspect"
Z_SCORE_THRESHOLD = 2.0

# Sur combien de matchs passés on calcule la moyenne "normale" d'une équipe
HISTORY_WINDOW = 10


def fd_get(endpoint: str, params: dict = None) -> dict:
    """Appel générique à l'API football-data.org"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    resp = requests.get(f"{FOOTBALL_API_BASE}{endpoint}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_finished_matches(competition_code: str, days_back: int = 14) -> list:
    """Récupère les matchs terminés récemment pour une compétition."""
    date_from = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    date_to = datetime.utcnow().strftime("%Y-%m-%d")
    data = fd_get(
        f"/competitions/{competition_code}/matches",
        params={"status": "FINISHED", "dateFrom": date_from, "dateTo": date_to},
    )
    return data.get("matches", [])


def get_team_recent_totals(competition_code: str, team_id: int, before_date: str) -> list:
    """Total de buts (marqués + encaissés) des N derniers matchs d'une équipe,
    avant une date donnée (pour ne pas inclure le match qu'on analyse)."""
    data = fd_get(
        f"/teams/{team_id}/matches",
        params={"status": "FINISHED", "limit": HISTORY_WINDOW + 5},
    )
    totals = []
    for m in data.get("matches", []):
        if m["utcDate"] >= before_date:
            continue
        home_goals = m["score"]["fullTime"]["home"]
        away_goals = m["score"]["fullTime"]["away"]
        if home_goals is None or away_goals is None:
            continue
        totals.append(home_goals + away_goals)
        if len(totals) >= HISTORY_WINDOW:
            break
    return totals


def analyze_match(match: dict, competition_code: str) -> dict | None:
    """Calcule le z-score du nombre de buts d'un match par rapport à
    l'historique récent des deux équipes. Retourne None si pas assez de
    données pour se prononcer."""
    home = match["homeTeam"]
    away = match["awayTeam"]
    score = match["score"]["fullTime"]
    if score["home"] is None or score["away"] is None:
        return None

    total_goals = score["home"] + score["away"]
    match_date = match["utcDate"]

    home_hist = get_team_recent_totals(competition_code, home["id"], match_date)
    away_hist = get_team_recent_totals(competition_code, away["id"], match_date)
    combined_hist = home_hist + away_hist

    if len(combined_hist) < 6:
        return None  # pas assez d'historique pour être fiable

    mean = statistics.mean(combined_hist)
    stdev = statistics.pstdev(combined_hist) or 0.5  # évite division par zéro
    z_score = (total_goals - mean) / stdev

    return {
        "home": home["name"],
        "away": away["name"],
        "score": f"{score['home']}-{score['away']}",
        "total_goals": total_goals,
        "expected_avg": round(mean, 2),
        "z_score": round(z_score, 2),
        "date": match_date[:10],
    }


def find_suspicious_matches(competition_code: str) -> list:
    matches = get_finished_matches(competition_code)
    results = []
    for m in matches:
        analysis = analyze_match(m, competition_code)
        if analysis and abs(analysis["z_score"]) >= Z_SCORE_THRESHOLD:
            results.append(analysis)
    results.sort(key=lambda x: abs(x["z_score"]), reverse=True)
    return results


# ---------------------- Handlers Telegram ----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Bot de détection d'anomalies de buts (football)\n\n"
        "Commandes disponibles :\n"
        "/check <code> - analyse une compétition (ex: /check PL)\n"
        "/ligues - liste des compétitions disponibles\n\n"
        "⚠️ Ceci détecte des écarts statistiques inhabituels, "
        "pas des preuves de matchs truqués."
    )
    await update.message.reply_text(text)


async def ligues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"{code} — {name}" for code, name in COMPETITIONS.items()]
    await update.message.reply_text("Compétitions disponibles :\n" + "\n".join(lines))


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : /check PL  (voir /ligues pour les codes)")
        return

    code = context.args[0].upper()
    if code not in COMPETITIONS:
        await update.message.reply_text(f"Code inconnu. Essaie /ligues pour voir la liste.")
        return

    await update.message.reply_text(f"🔍 Analyse en cours pour {COMPETITIONS[code]}...")

    try:
        suspects = find_suspicious_matches(code)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Erreur API : {e}")
        return

    if not suspects:
        await update.message.reply_text("✅ Aucun match hors norme détecté récemment.")
        return

    lines = [f"⚠️ {len(suspects)} match(s) avec un nombre de buts inhabituel :\n"]
    for s in suspects[:10]:
        lines.append(
            f"• {s['date']} — {s['home']} {s['score']} {s['away']}\n"
            f"  Total buts: {s['total_goals']} (moyenne attendue: {s['expected_avg']}, z-score: {s['z_score']})"
        )
    await update.message.reply_text("\n\n".join(lines))


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Variable d'environnement TELEGRAM_BOT_TOKEN manquante.")
    if not FOOTBALL_API_KEY:
        raise SystemExit("Variable d'environnement FOOTBALL_DATA_API_KEY manquante.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ligues", ligues))
    app.add_handler(CommandHandler("check", check))

    logger.info("Bot démarré, en écoute...")
    app.run_polling()


if __name__ == "__main__":
    main()
