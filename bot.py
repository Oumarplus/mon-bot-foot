"""
Bot Telegram - Détection de matchs de football avec des schémas de buts anormaux
==================================================================================

Trois signaux statistiques combinés :
1. Nombre total de buts vs moyenne récente des deux équipes (z-score)
2. Répartition 1ère/2ème mi-temps anormalement déséquilibrée
3. Écart par rapport à l'historique des confrontations directes (face-à-face)

Plus une alerte automatique quotidienne (/subscribe).

⚠️ IMPORTANT : ceci est un outil d'analyse statistique, PAS une preuve de
match truqué. Un score élevé peut avoir plein d'explications légitimes
(tactique, forme du moment, cartons rouges, météo...). À utiliser comme
signal d'alerte à recouper, jamais comme accusation.

Installation : voir README.md
"""

import os
import json
import logging
import statistics
from datetime import datetime, timedelta, time as dtime

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

Z_SCORE_THRESHOLD = 2.0        # seuil du signal "nombre de buts"
HISTORY_WINDOW = 10            # nb de matchs passés pour la moyenne d'une équipe
SECOND_HALF_SHARE_THRESHOLD = 0.75  # part de buts en 2e mi-temps jugée anormale
SECOND_HALF_MIN_GOALS = 3      # nb minimum de buts pour que ce signal soit pertinent
H2H_MIN_MATCHES = 3            # nb minimum de confrontations directes pour ce signal
SUBSCRIBERS_FILE = "subscribers.json"
DAILY_ALERT_HOUR_UTC = 8       # heure (UTC) d'envoi de l'alerte quotidienne


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


def get_team_recent_totals(team_id: int, before_date: str) -> list:
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


def analyze_goal_count(match: dict) -> dict | None:
    """Signal 1 : z-score du nombre total de buts vs historique récent des
    deux équipes."""
    home, away = match["homeTeam"], match["awayTeam"]
    score = match["score"]["fullTime"]
    if score["home"] is None or score["away"] is None:
        return None

    total_goals = score["home"] + score["away"]
    match_date = match["utcDate"]

    home_hist = get_team_recent_totals(home["id"], match_date)
    away_hist = get_team_recent_totals(away["id"], match_date)
    combined_hist = home_hist + away_hist

    if len(combined_hist) < 6:
        return None

    mean = statistics.mean(combined_hist)
    stdev = statistics.pstdev(combined_hist) or 0.5
    z_score = (total_goals - mean) / stdev

    if abs(z_score) < Z_SCORE_THRESHOLD:
        return None

    return {
        "type": "buts",
        "detail": f"Total buts: {total_goals} (moyenne attendue: {round(mean, 2)}, z-score: {round(z_score, 2)})",
    }


def analyze_half_split(match: dict) -> dict | None:
    """Signal 2 : répartition anormale des buts entre les deux mi-temps
    (beaucoup de buts tardifs concentrés en 2e période)."""
    full = match["score"]["fullTime"]
    half = match["score"].get("halfTime", {})
    if full["home"] is None or full["away"] is None:
        return None
    if half.get("home") is None or half.get("away") is None:
        return None

    total_goals = full["home"] + full["away"]
    first_half_goals = half["home"] + half["away"]
    second_half_goals = total_goals - first_half_goals

    if total_goals < SECOND_HALF_MIN_GOALS:
        return None

    second_half_share = second_half_goals / total_goals
    if second_half_share < SECOND_HALF_SHARE_THRESHOLD:
        return None

    return {
        "type": "mi-temps",
        "detail": f"{second_half_goals}/{total_goals} buts en 2e mi-temps ({round(second_half_share * 100)}%)",
    }


def analyze_head_to_head(match: dict) -> dict | None:
    """Signal 3 : écart par rapport à l'historique des confrontations
    directes entre ces deux équipes précises."""
    full = match["score"]["fullTime"]
    if full["home"] is None or full["away"] is None:
        return None
    total_goals = full["home"] + full["away"]

    try:
        data = fd_get(f"/matches/{match['id']}/head2head", params={"limit": 10})
    except requests.HTTPError:
        return None

    past_totals = []
    for m in data.get("matches", []):
        if m["id"] == match["id"]:
            continue
        s = m["score"]["fullTime"]
        if s["home"] is None or s["away"] is None:
            continue
        past_totals.append(s["home"] + s["away"])

    if len(past_totals) < H2H_MIN_MATCHES:
        return None

    mean = statistics.mean(past_totals)
    stdev = statistics.pstdev(past_totals) or 0.5
    z_score = (total_goals - mean) / stdev

    if abs(z_score) < Z_SCORE_THRESHOLD:
        return None

    return {
        "type": "face-à-face",
        "detail": f"Historique direct: {round(mean, 2)} buts en moyenne sur {len(past_totals)} confrontations (z-score: {round(z_score, 2)})",
    }


def analyze_match(match: dict) -> dict | None:
    """Combine les 3 signaux. Retourne un résumé si au moins un signal se
    déclenche."""
    home, away = match["homeTeam"], match["awayTeam"]
    score = match["score"]["fullTime"]
    if score["home"] is None or score["away"] is None:
        return None

    signals = []
    for fn in (analyze_goal_count, analyze_half_split, analyze_head_to_head):
        try:
            result = fn(match)
        except requests.HTTPError:
            result = None
        if result:
            signals.append(result)

    if not signals:
        return None

    return {
        "home": home["name"],
        "away": away["name"],
        "score": f"{score['home']}-{score['away']}",
        "date": match["utcDate"][:10],
        "signals": signals,
    }


def find_suspicious_matches(competition_code: str) -> list:
    matches = get_finished_matches(competition_code)
    results = [analyze_match(m) for m in matches]
    results = [r for r in results if r]
    results.sort(key=lambda x: len(x["signals"]), reverse=True)
    return results


def format_match_report(s: dict) -> str:
    lines = [f"• {s['date']} — {s['home']} {s['score']} {s['away']}"]
    for sig in s["signals"]:
        lines.append(f"   ↳ [{sig['type']}] {sig['detail']}")
    return "\n".join(lines)


# ---------------------- Abonnés à l'alerte quotidienne ----------------------

def load_subscribers() -> set:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return set()
    with open(SUBSCRIBERS_FILE, "r") as f:
        return set(json.load(f))


def save_subscribers(chat_ids: set):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(chat_ids), f)


# ---------------------- Handlers Telegram ----------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Bot de détection d'anomalies de buts (football)\n\n"
        "Commandes disponibles :\n"
        "/check <code> - analyse une compétition (ex: /check PL)\n"
        "/ligues - liste des compétitions disponibles\n"
        "/subscribe - reçois une alerte chaque jour\n"
        "/unsubscribe - stoppe l'alerte quotidienne\n\n"
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
        await update.message.reply_text("Code inconnu. Essaie /ligues pour voir la liste.")
        return

    await update.message.reply_text(f"🔍 Analyse en cours pour {COMPETITIONS[code]} (3 signaux)...")

    try:
        suspects = find_suspicious_matches(code)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Erreur API : {e}")
        return

    if not suspects:
        await update.message.reply_text("✅ Aucun match hors norme détecté récemment.")
        return

    lines = [f"⚠️ {len(suspects)} match(s) avec au moins un signal inhabituel :\n"]
    for s in suspects[:8]:
        lines.append(format_match_report(s))
    await update.message.reply_text("\n\n".join(lines))


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    subs.add(chat_id)
    save_subscribers(subs)
    await update.message.reply_text(
        f"✅ Abonné ! Tu recevras une alerte chaque jour vers {DAILY_ALERT_HOUR_UTC}h (heure UTC) "
        "s'il y a des matchs suspects."
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    subs.discard(chat_id)
    save_subscribers(subs)
    await update.message.reply_text("❌ Désabonné de l'alerte quotidienne.")


async def send_daily_alert(context: ContextTypes.DEFAULT_TYPE):
    subs = load_subscribers()
    if not subs:
        return

    all_suspects = []
    for code, name in COMPETITIONS.items():
        try:
            suspects = find_suspicious_matches(code)
        except requests.HTTPError:
            continue
        for s in suspects:
            s["competition"] = name
            all_suspects.append(s)

    if not all_suspects:
        return  # rien d'anormal, pas de notification pour ne pas spammer

    lines = [f"📅 Alerte quotidienne — {len(all_suspects)} match(s) suspect(s) :\n"]
    for s in all_suspects[:10]:
        lines.append(f"[{s['competition']}]\n" + format_match_report(s))
    text = "\n\n".join(lines)

    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Échec envoi à {chat_id}: {e}")


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Variable d'environnement TELEGRAM_BOT_TOKEN manquante.")
    if not FOOTBALL_API_KEY:
        raise SystemExit("Variable d'environnement FOOTBALL_DATA_API_KEY manquante.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ligues", ligues))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))

    app.job_queue.run_daily(send_daily_alert, time=dtime(hour=DAILY_ALERT_HOUR_UTC, minute=0))

    logger.info("Bot démarré, en écoute...")
    app.run_polling()


if __name__ == "__main__":
    main()
