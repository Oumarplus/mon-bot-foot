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
import math
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

# Deuxième API (optionnelle) pour les blessures/suspensions — api-football.com
# Si la clé n'est pas configurée, cette fonctionnalité est simplement désactivée.
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"

# Troisième API (optionnelle) pour le suivi des mouvements de cotes —
# the-odds-api.com. Plan gratuit : 500 requêtes/mois, donc uniquement
# consultable manuellement via /odds (pas de surveillance automatique).
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_STATE_FILE = "odds_state.json"
ODDS_MOVE_THRESHOLD = 0.08  # 8 points de probabilité implicite = mouvement notable

# Nos codes de compétition -> clé "sport" attendue par The Odds API
ODDS_SPORT_KEYS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "FL1": "soccer_france_ligue_one",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "CL": "soccer_uefa_champs_league",
    "BSA": "soccer_brazil_campeonato",
    "DED": "soccer_netherlands_eredivisie",
}

# Compétitions supportées par le plan gratuit de football-data.org
COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga",
    "FL1": "Ligue 1",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "CL": "Ligue des Champions",
    "BSA": "Brasileirão (Brésil)",
    "DED": "Eredivisie (Pays-Bas)",
}

Z_SCORE_THRESHOLD = 2.0        # seuil du signal "nombre de buts"
HISTORY_WINDOW = 10            # nb de matchs passés pour la moyenne d'une équipe
SECOND_HALF_SHARE_THRESHOLD = 0.75  # part de buts en 2e mi-temps jugée anormale
SECOND_HALF_MIN_GOALS = 3      # nb minimum de buts pour que ce signal soit pertinent
H2H_MIN_MATCHES = 3            # nb minimum de confrontations directes pour ce signal
SUBSCRIBERS_FILE = "subscribers.json"
DAILY_ALERT_HOUR_UTC = 8       # heure (UTC) d'envoi de l'alerte quotidienne

PREDICT_DAYS_AHEAD = 7         # horizon pour /predict (matchs à venir)
PREDICT_HISTORY_WINDOW = 8     # nb de matchs récents utilisés pour estimer la forme
GOAL_LINES = [1.5, 2.5, 3.5]   # lignes Plus/Moins affichées
MAX_GOALS_SIMULATED = 8        # buts max simulés par équipe dans le modèle de Poisson

LIVE_STATE_FILE = "live_state.json"
LIVE_SUBSCRIBERS_FILE = "live_subscribers.json"
LIVE_CHECK_INTERVAL_MINUTES = 10   # fréquence de la surveillance en direct
LIVE_BURST_GOALS_THRESHOLD = 2     # nb de buts rapprochés jugé "sursaut suspect"
LIVE_BURST_MINUTES_WINDOW = 15     # sur combien de minutes ce sursaut est mesuré


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


# ---------------------- Prédictions (matchs à venir) ----------------------

def get_upcoming_matches(competition_code: str, days_ahead: int = PREDICT_DAYS_AHEAD) -> list:
    """Récupère les prochains matchs programmés d'une compétition."""
    date_from = datetime.utcnow().strftime("%Y-%m-%d")
    date_to = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    data = fd_get(
        f"/competitions/{competition_code}/matches",
        params={"status": "SCHEDULED", "dateFrom": date_from, "dateTo": date_to},
    )
    return data.get("matches", [])


def get_team_scored_conceded(team_id: int, is_home: bool, limit: int = PREDICT_HISTORY_WINDOW) -> tuple:
    """Moyenne de buts marqués et encaissés par une équipe sur ses N derniers
    matchs, en tenant compte du fait qu'elle joue à domicile ou à l'extérieur
    (les équipes marquent en moyenne plus à domicile). Retourne aussi la
    forme (V/N/D) sur ces mêmes matchs, du plus récent au plus ancien."""
    data = fd_get(
        f"/teams/{team_id}/matches",
        params={"status": "FINISHED", "limit": limit + 10},
    )
    matches = data.get("matches", [])
    matches.sort(key=lambda m: m["utcDate"], reverse=True)  # plus récent d'abord

    scored, conceded, form = [], [], []
    for m in matches:
        home_goals = m["score"]["fullTime"]["home"]
        away_goals = m["score"]["fullTime"]["away"]
        if home_goals is None or away_goals is None:
            continue
        team_is_home = m["homeTeam"]["id"] == team_id
        if team_is_home != is_home:
            continue  # on ne garde que les matchs joués dans le même contexte (dom./ext.)
        team_goals = home_goals if team_is_home else away_goals
        opp_goals = away_goals if team_is_home else home_goals
        scored.append(team_goals)
        conceded.append(opp_goals)
        if team_goals > opp_goals:
            form.append("V")
        elif team_goals == opp_goals:
            form.append("N")
        else:
            form.append("D")
        if len(scored) >= limit:
            break
    return scored, conceded, form


def poisson_pmf(k: int, lam: float) -> float:
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def build_score_matrix(lambda_home: float, lambda_away: float) -> list:
    """Matrice de probabilité P(home=i, away=j) pour i,j de 0 à MAX_GOALS_SIMULATED,
    en supposant les buts de chaque équipe indépendants et suivant une loi de
    Poisson (modèle standard pour ce type d'estimation)."""
    matrix = []
    for i in range(MAX_GOALS_SIMULATED + 1):
        row = []
        for j in range(MAX_GOALS_SIMULATED + 1):
            row.append(poisson_pmf(i, lambda_home) * poisson_pmf(j, lambda_away))
        matrix.append(row)
    return matrix


_elo_cache = {}  # nom d'équipe -> rating Elo (évite de re-télécharger le CSV)

# Bornes approximatives du rating Elo clubelo, tous clubs du monde confondus
# (clubs faibles ~1300, meilleurs clubs mondiaux ~2100). Utilisées uniquement
# pour l'affichage, converti sur une échelle 1-99 plus lisible.
ELO_DISPLAY_FLOOR = 1300
ELO_DISPLAY_CEILING = 2100


def scale_elo_for_display(raw_elo: float) -> int:
    """Convertit un rating Elo brut clubelo en une note sur ~1-99, pour un
    affichage plus intuitif (ex: 86 au lieu de 1923)."""
    pct = (raw_elo - ELO_DISPLAY_FLOOR) / (ELO_DISPLAY_CEILING - ELO_DISPLAY_FLOOR)
    pct = min(max(pct, 0.0), 1.0)
    return round(1 + pct * 98)


def get_team_elo(team_name: str) -> float | None:
    """Récupère le rating Elo actuel d'une équipe via clubelo.com (API
    publique gratuite, aucune clé nécessaire). Les noms d'équipes de
    clubelo diffèrent parfois de ceux de football-data.org (ex: 'Manchester
    City' -> 'ManCity'), donc on essaie plusieurs variantes du nom."""
    if team_name in _elo_cache:
        return _elo_cache[team_name]

    candidates = [
        team_name,
        team_name.replace(" ", ""),
        team_name.replace(" FC", "").replace("FC ", "").replace(" ", ""),
    ]
    for name in candidates:
        try:
            resp = requests.get(f"http://api.clubelo.com/{name}", timeout=10)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) < 2:
                continue
            header = lines[0].split(",")
            last_row = lines[-1].split(",")
            row = dict(zip(header, last_row))
            elo = float(row["Elo"])
            _elo_cache[team_name] = elo
            return elo
        except (requests.RequestException, ValueError, KeyError):
            continue
    _elo_cache[team_name] = None
    return None


def get_team_elo_history(team_name: str, days: int = 365) -> list:
    """Récupère l'historique complet du rating Elo d'une équipe sur les N
    derniers jours (courbe clubelo). Retourne une liste de (date, elo)."""
    candidates = [
        team_name,
        team_name.replace(" ", ""),
        team_name.replace(" FC", "").replace("FC ", "").replace(" ", ""),
    ]
    cutoff = datetime.utcnow() - timedelta(days=days)

    for name in candidates:
        try:
            resp = requests.get(f"http://api.clubelo.com/{name}", timeout=15)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            if len(lines) < 2:
                continue
            header = lines[0].split(",")
            history = []
            for line in lines[1:]:
                row = dict(zip(header, line.split(",")))
                try:
                    row_date = datetime.strptime(row["From"], "%Y-%m-%d")
                except (ValueError, KeyError):
                    continue
                if row_date < cutoff:
                    continue
                history.append((row_date, float(row["Elo"])))
            if history:
                return history
        except (requests.RequestException, ValueError, KeyError):
            continue
    return []


def generate_elo_chart(home_name: str, away_name: str, days: int = 365) -> str | None:
    """Génère un graphique PNG de la progression Elo de deux équipes sur les
    N derniers jours et retourne le chemin du fichier, ou None si aucune
    donnée n'a été trouvée pour les deux équipes."""
    home_history = get_team_elo_history(home_name, days)
    away_history = get_team_elo_history(away_name, days)

    if not home_history and not away_history:
        return None

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 5))

    if home_history:
        dates, elos = zip(*home_history)
        ax.plot(dates, elos, label=home_name, color="#2a9d8f", linewidth=2)
    if away_history:
        dates, elos = zip(*away_history)
        ax.plot(dates, elos, label=away_name, color="#e76f51", linewidth=2)

    ax.set_title(f"Progression Élo — {home_name} vs {away_name}")
    ax.set_ylabel("Élo")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()

    path = "/tmp/elo_chart.png"
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path


def predict_match(match: dict) -> dict | None:
    """Estime, pour un match à venir, les probabilités Plus/Moins, BTTS et
    1X2 à partir de la forme récente (domicile/extérieur) des deux équipes."""
    home, away = match["homeTeam"], match["awayTeam"]

    home_scored, home_conceded, home_form = get_team_scored_conceded(home["id"], is_home=True)
    away_scored, away_conceded, away_form = get_team_scored_conceded(away["id"], is_home=False)

    if len(home_scored) < 3 or len(away_scored) < 3:
        return None  # pas assez de matchs dans ce contexte pour être fiable

    # lambda = buts attendus, moyenne entre "l'attaque de l'un" et "la défense de l'autre"
    lambda_home = (statistics.mean(home_scored) + statistics.mean(away_conceded)) / 2
    lambda_away = (statistics.mean(away_scored) + statistics.mean(home_conceded)) / 2
    lambda_home = max(lambda_home, 0.1)
    lambda_away = max(lambda_away, 0.1)

    matrix = build_score_matrix(lambda_home, lambda_away)

    p_home_win = sum(matrix[i][j] for i in range(len(matrix)) for j in range(len(matrix)) if i > j)
    p_draw = sum(matrix[i][i] for i in range(len(matrix)))
    p_away_win = sum(matrix[i][j] for i in range(len(matrix)) for j in range(len(matrix)) if i < j)

    p_home_no_goal = poisson_pmf(0, lambda_home)
    p_away_no_goal = poisson_pmf(0, lambda_away)
    p_btts_yes = (1 - p_home_no_goal) * (1 - p_away_no_goal)

    over_under = {}
    for line in GOAL_LINES:
        threshold = math.floor(line)  # ex: 2.5 -> plus de 2 buts = "plus de 2.5"
        p_under = sum(
            matrix[i][j]
            for i in range(len(matrix))
            for j in range(len(matrix))
            if i + j <= threshold
        )
        over_under[line] = {"over": round((1 - p_under) * 100, 1), "under": round(p_under * 100, 1)}

    # --- Scores exacts les plus probables (top 3) ---
    scorelines = []
    for i in range(len(matrix)):
        for j in range(len(matrix)):
            scorelines.append(((i, j), matrix[i][j]))
    scorelines.sort(key=lambda x: x[1], reverse=True)
    top_scores = [
        {"score": f"{i}-{j}", "probability": round(prob * 100, 1)}
        for (i, j), prob in scorelines[:3]
    ]

    # --- Scores groupés par catégorie ---
    def group_prob(scores: list) -> float:
        total = 0.0
        for s in scores:
            i, j = map(int, s.split("-"))
            if i <= len(matrix) - 1 and j <= len(matrix) - 1:
                total += matrix[i][j]
        return round(total * 100, 1)

    score_groups = {
        "Victoire dom. nette (1-0, 2-0, 3-0)": group_prob(["1-0", "2-0", "3-0"]),
        "Match nul (1-1, 2-2, 3-3)": group_prob(["1-1", "2-2", "3-3"]),
        "Victoire dom. courte (2-1, 3-1, 4-1)": group_prob(["2-1", "3-1", "4-1"]),
        "Victoire ext. nette (0-1, 0-2, 0-3)": group_prob(["0-1", "0-2", "0-3"]),
        "Victoire ext. courte (1-2, 1-3, 1-4)": group_prob(["1-2", "1-3", "1-4"]),
    }

    # --- Elo (force relative des équipes) ---
    elo_home = get_team_elo(home["name"])
    elo_away = get_team_elo(away["name"])

    # --- Blessures / suspensions (best effort, peut être None) ---
    match_date = match["utcDate"][:10]
    absences = get_absences(home["name"], away["name"], match_date)

    return {
        "home": home["name"],
        "away": away["name"],
        "date": match["utcDate"][:16].replace("T", " "),
        "lambda_home": round(lambda_home, 2),
        "lambda_away": round(lambda_away, 2),
        "p_home_win": round(p_home_win * 100, 1),
        "p_draw": round(p_draw * 100, 1),
        "p_away_win": round(p_away_win * 100, 1),
        "p_btts_yes": round(p_btts_yes * 100, 1),
        "over_under": over_under,
        "top_scores": top_scores,
        "score_groups": score_groups,
        "home_form": "-".join(home_form[:5]) if home_form else None,
        "away_form": "-".join(away_form[:5]) if away_form else None,
        "elo_home": round(elo_home) if elo_home else None,
        "elo_away": round(elo_away) if elo_away else None,
        "absences": absences,
    }


def format_prediction(p: dict) -> str:
    lines = [f"⚽ {p['date']} — {p['home']} vs {p['away']}"]

    if p["elo_home"] and p["elo_away"]:
        elo_home_display = scale_elo_for_display(p["elo_home"])
        elo_away_display = scale_elo_for_display(p["elo_away"])
        lines.append(f"   Élo: {p['home']} ({elo_home_display}) — {p['away']} ({elo_away_display})")

    if p["home_form"] or p["away_form"]:
        lines.append(
            f"   Forme (5 derniers, même contexte dom./ext.): "
            f"{p['home']} [{p['home_form'] or '?'}] | {p['away']} [{p['away_form'] or '?'}]"
        )

    lines.append(f"   Buts attendus (modèle): {p['home']} {p['lambda_home']} — {p['lambda_away']} {p['away']}")
    lines.append(f"   1X2: Dom {p['p_home_win']}% | Nul {p['p_draw']}% | Ext {p['p_away_win']}%")
    lines.append(f"   Les 2 équipes marquent: Oui {p['p_btts_yes']}% | Non {round(100 - p['p_btts_yes'], 1)}%")
    for line, vals in p["over_under"].items():
        lines.append(f"   Total buts {line}: Plus {vals['over']}% | Moins {vals['under']}%")

    scores_txt = ", ".join(f"{s['score']} ({s['probability']}%)" for s in p["top_scores"])
    lines.append(f"   Scores les plus probables: {scores_txt}")

    lines.append("   Groupes de scores:")
    for label, pct in p["score_groups"].items():
        lines.append(f"     • {label}: {pct}%")

    if p["absences"] is not None:
        lines.append(format_absences(p["absences"]))

    return "\n".join(lines)
    return "\n".join(lines)


# ---------------------- Blessures / suspensions (API-Football) ----------------------

_team_id_cache = {}  # nom d'équipe -> id API-Football (évite de re-chercher à chaque fois)


def af_get(endpoint: str, params: dict = None):
    """Appel générique à l'API-Football. Retourne None si la clé n'est pas
    configurée ou si l'appel échoue (quota dépassé, etc.) — cette
    fonctionnalité est toujours "best effort", elle ne doit jamais faire
    planter le bot."""
    if not API_FOOTBALL_KEY:
        return None
    headers = {"x-apisports-key": API_FOOTBALL_KEY}
    try:
        resp = requests.get(f"{API_FOOTBALL_BASE}{endpoint}", headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def af_find_team_id(team_name: str) -> int | None:
    if team_name in _team_id_cache:
        return _team_id_cache[team_name]
    data = af_get("/teams", params={"search": team_name})
    if not data or not data.get("response"):
        return None
    team_id = data["response"][0]["team"]["id"]
    _team_id_cache[team_name] = team_id
    return team_id


def af_find_fixture_id(home_team_id: int, match_date: str) -> int | None:
    """match_date au format YYYY-MM-DD."""
    data = af_get("/fixtures", params={"team": home_team_id, "date": match_date})
    if not data or not data.get("response"):
        return None
    return data["response"][0]["fixture"]["id"]


def get_absences(home_name: str, away_name: str, match_date: str) -> list | None:
    """Liste des joueurs blessés/suspendus pour un match donné. Retourne None
    si l'info n'est pas disponible (clé absente, équipe non trouvée, quota
    dépassé...) plutôt que de lever une erreur."""
    if not API_FOOTBALL_KEY:
        return None

    home_id = af_find_team_id(home_name)
    if not home_id:
        return None

    fixture_id = af_find_fixture_id(home_id, match_date)
    if not fixture_id:
        return None

    data = af_get("/injuries", params={"fixture": fixture_id})
    if not data:
        return None

    absences = []
    for entry in data.get("response", []):
        player = entry.get("player", {})
        team = entry.get("team", {})
        absences.append({
            "player": player.get("name", "?"),
            "team": team.get("name", "?"),
            "reason": player.get("reason") or player.get("type") or "raison inconnue",
        })
    return absences


def format_absences(absences: list) -> str:
    if not absences:
        return "   Effectifs: aucune absence signalée"
    lines = ["   Absences signalées:"]
    for a in absences:
        lines.append(f"     • {a['player']} ({a['team']}) — {a['reason']}")
    return "\n".join(lines)


# ---------------------- Surveillance en direct (matchs en cours) ----------------------

def get_live_matches(competition_code: str) -> list:
    """Récupère les matchs actuellement en cours (mi-temps comprise) d'une
    compétition."""
    data = fd_get(
        f"/competitions/{competition_code}/matches",
        params={"status": "LIVE"},
    )
    return data.get("matches", [])


def load_live_state() -> dict:
    if not os.path.exists(LIVE_STATE_FILE):
        return {}
    with open(LIVE_STATE_FILE, "r") as f:
        return json.load(f)


def save_live_state(state: dict):
    with open(LIVE_STATE_FILE, "w") as f:
        json.dump(state, f)


def load_live_subscribers() -> set:
    if not os.path.exists(LIVE_SUBSCRIBERS_FILE):
        return set()
    with open(LIVE_SUBSCRIBERS_FILE, "r") as f:
        return set(json.load(f))


def save_live_subscribers(chat_ids: set):
    with open(LIVE_SUBSCRIBERS_FILE, "w") as f:
        json.dump(list(chat_ids), f)


def check_live_match_for_burst(match: dict, state: dict) -> dict | None:
    """Compare le score actuel d'un match en direct à son dernier score
    connu (mémorisé lors du passage précédent). Si plusieurs buts sont
    tombés d'un coup sur une courte fenêtre, retourne une alerte.

    ⚠️ Cette détection dépend de la fréquence de passage du bot (toutes les
    ~10 min) et des données football-data.org, qui peuvent être retardées de
    quelques minutes par rapport au direct réel — ce n'est pas un flux temps
    réel garanti."""
    match_id = str(match["id"])
    score = match["score"]["fullTime"]
    home_goals, away_goals = score.get("home"), score.get("away")
    if home_goals is None or away_goals is None:
        return None
    current_total = home_goals + away_goals
    current_minute = match.get("minute")
    now = datetime.utcnow().isoformat()

    previous = state.get(match_id)
    state[match_id] = {
        "total_goals": current_total,
        "checked_at": now,
        "minute": current_minute,
    }

    if not previous:
        return None  # première fois qu'on voit ce match, pas de comparaison possible

    goals_since_last = current_total - previous["total_goals"]
    if goals_since_last < LIVE_BURST_GOALS_THRESHOLD:
        return None

    minutes_elapsed = None
    if current_minute is not None and previous.get("minute") is not None:
        minutes_elapsed = current_minute - previous["minute"]

    if minutes_elapsed is not None and minutes_elapsed > LIVE_BURST_MINUTES_WINDOW:
        return None  # trop étalé dans le temps pour être un "sursaut"

    return {
        "home": match["homeTeam"]["name"],
        "away": match["awayTeam"]["name"],
        "score": f"{home_goals}-{away_goals}",
        "goals_since_last": goals_since_last,
        "minute": current_minute,
    }


async def check_live_burst_job(context: ContextTypes.DEFAULT_TYPE):
    """Job périodique : parcourt les matchs en direct de toutes les
    compétitions et alerte les abonnés en cas de sursaut de buts suspect."""
    subs = load_live_subscribers()
    if not subs:
        return

    state = load_live_state()
    alerts = []

    for code, name in COMPETITIONS.items():
        try:
            matches = get_live_matches(code)
        except requests.HTTPError:
            continue
        for m in matches:
            alert = check_live_match_for_burst(m, state)
            if alert:
                alert["competition"] = name
                alerts.append(alert)

    save_live_state(state)

    if not alerts:
        return

    lines = ["🔴 Sursaut de buts détecté en direct :\n"]
    for a in alerts:
        lines.append(
            f"[{a['competition']}] {a['home']} {a['score']} {a['away']} "
            f"— {a['goals_since_last']} but(s) rapprochés (minute ~{a['minute']})"
        )
    text = "\n".join(lines)

    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"Échec envoi alerte live à {chat_id}: {e}")


# ---------------------- Mouvements de cotes (The Odds API) ----------------------

def odds_get(endpoint: str, params: dict = None):
    """Appel générique à The Odds API. Retourne None si la clé n'est pas
    configurée ou si l'appel échoue — cette fonctionnalité est "best effort"
    et ne doit jamais faire planter le bot."""
    if not ODDS_API_KEY:
        return None
    params = dict(params or {})
    params["apiKey"] = ODDS_API_KEY
    try:
        resp = requests.get(f"{ODDS_API_BASE}{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def get_current_odds(sport_key: str) -> list:
    """Récupère les cotes actuelles (1X2 + totaux) pour tous les matchs à
    venir d'un championnat, moyennées sur les bookmakers disponibles."""
    data = odds_get(
        f"/sports/{sport_key}/odds",
        params={"regions": "eu,uk", "markets": "h2h,totals", "oddsFormat": "decimal"},
    )
    return data or []


def implied_probabilities_h2h(event: dict) -> dict | None:
    """Moyenne, sur tous les bookmakers d'un événement, des probabilités
    implicites 1X2 (normalisées pour retirer la marge du bookmaker)."""
    home_probs, draw_probs, away_probs = [], [], []
    home_name, away_name = event.get("home_team"), event.get("away_team")

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
            if home_name not in outcomes or away_name not in outcomes:
                continue
            raw = {k: 1 / v for k, v in outcomes.items() if v}
            total = sum(raw.values())
            if total == 0:
                continue
            home_probs.append(raw.get(home_name, 0) / total)
            away_probs.append(raw.get(away_name, 0) / total)
            draw_probs.append(raw.get("Draw", 0) / total)

    if not home_probs:
        return None

    return {
        "home": round(statistics.mean(home_probs), 3),
        "draw": round(statistics.mean(draw_probs), 3) if draw_probs else None,
        "away": round(statistics.mean(away_probs), 3),
    }


def load_odds_state() -> dict:
    if not os.path.exists(ODDS_STATE_FILE):
        return {}
    with open(ODDS_STATE_FILE, "r") as f:
        return json.load(f)


def save_odds_state(state: dict):
    with open(ODDS_STATE_FILE, "w") as f:
        json.dump(state, f)


def check_odds_movement(competition_code: str) -> list:
    """Compare les cotes actuelles d'un championnat à celles mémorisées lors
    du précédent /odds, et retourne la liste des matchs avec un mouvement
    de probabilité implicite notable (>= ODDS_MOVE_THRESHOLD)."""
    sport_key = ODDS_SPORT_KEYS.get(competition_code)
    if not sport_key:
        return []

    events = get_current_odds(sport_key)
    state = load_odds_state()
    results = []

    for event in events:
        event_id = event["id"]
        current = implied_probabilities_h2h(event)
        if not current:
            continue

        previous = state.get(event_id)
        state[event_id] = {
            "probabilities": current,
            "checked_at": datetime.utcnow().isoformat(),
            "home_team": event.get("home_team"),
            "away_team": event.get("away_team"),
        }

        entry = {
            "home": event.get("home_team"),
            "away": event.get("away_team"),
            "current": current,
            "movement": None,
        }

        if previous:
            prev_probs = previous["probabilities"]
            deltas = {
                k: round(current[k] - prev_probs[k], 3)
                for k in ("home", "draw", "away")
                if current.get(k) is not None and prev_probs.get(k) is not None
            }
            if deltas and max(abs(v) for v in deltas.values()) >= ODDS_MOVE_THRESHOLD:
                entry["movement"] = deltas

        results.append(entry)

    save_odds_state(state)
    return results


def format_odds_report(entries: list) -> str:
    lines = []
    for e in entries:
        c = e["current"]
        line = f"• {e['home']} vs {e['away']} — Dom {round(c['home']*100)}%"
        if c.get("draw") is not None:
            line += f" | Nul {round(c['draw']*100)}%"
        line += f" | Ext {round(c['away']*100)}%"
        lines.append(line)
        if e["movement"]:
            m = e["movement"]
            parts = [f"{k}: {'+' if v >= 0 else ''}{round(v*100)}pt" for k, v in m.items()]
            lines.append(f"   ⚠️ Mouvement notable depuis la dernière vérification: {', '.join(parts)}")
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
        "/predict <code> - pronostics sur les matchs à venir (ex: /predict PL)\n"
        "/ligues - liste des compétitions disponibles\n"
        "/subscribe - reçois une alerte chaque jour\n"
        "/unsubscribe - stoppe l'alerte quotidienne\n"
        "/live <code> - vérifie les matchs en direct maintenant (ex: /live PL)\n"
        "/live_subscribe - surveillance auto des matchs en cours (sursauts de buts)\n"
        "/live_unsubscribe - stoppe cette surveillance\n"
        "/odds <code> - vérifie les cotes et leur mouvement (ex: /odds PL)\n"
        "/oddsnow <code> - juste les probabilités 1X2 actuelles, sans suivi (ex: /oddsnow PL)\n\n"
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


async def predict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Utilisation : /predict PL  (voir /ligues pour les codes)")
        return

    code = context.args[0].upper()
    if code not in COMPETITIONS:
        await update.message.reply_text("Code inconnu. Essaie /ligues pour voir la liste.")
        return

    await update.message.reply_text(
        f"🔮 Calcul des pronostics pour {COMPETITIONS[code]} (matchs des {PREDICT_DAYS_AHEAD} prochains jours)..."
    )

    try:
        matches = get_upcoming_matches(code)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Erreur API : {e}")
        return

    if not matches:
        await update.message.reply_text("Aucun match programmé dans les prochains jours pour cette compétition.")
        return

    predictions = []
    for m in matches:
        try:
            p = predict_match(m)
        except requests.HTTPError:
            p = None
        if p:
            predictions.append(p)

    if not predictions:
        await update.message.reply_text(
            "Pas assez d'historique domicile/extérieur pour ces équipes pour établir un pronostic fiable."
        )
        return

    lines = [f"📊 Pronostics — {len(predictions)} match(s) :\n"]
    for p in predictions[:8]:
        lines.append(format_prediction(p))
    text = "\n\n".join(lines)

    # Telegram limite un message à 4096 caractères : on découpe si besoin
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def test_elo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande de debug temporaire : /elo Real Madrid"""
    if not context.args:
        await update.message.reply_text("Utilisation : /elo Real Madrid")
        return
    team_name = " ".join(context.args)
    elo = get_team_elo(team_name)
    if elo is None:
        await update.message.reply_text(f"Aucun rating Elo trouvé pour '{team_name}'.")
    else:
        await update.message.reply_text(
            f"Elo de '{team_name}': {round(elo)} (note sur 99: {scale_elo_for_display(elo)})"
        )


async def elochart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envoie un graphique de progression Élo sur 12 mois pour 1 ou 2 équipes.
    Utilisation : /elochart Real Madrid ; Barcelona"""
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Utilisation : /elochart Real Madrid ; Barcelona")
        return

    parts = [p.strip() for p in text.split(";")]
    home_name = parts[0]
    away_name = parts[1] if len(parts) > 1 else parts[0]

    await update.message.reply_text("📈 Génération du graphique...")

    try:
        path = generate_elo_chart(home_name, away_name)
    except Exception as e:
        logger.warning(f"Erreur génération graphique Elo: {e}")
        path = None

    if not path:
        await update.message.reply_text("Aucune donnée Élo trouvée pour ces équipes.")
        return

    with open(path, "rb") as f:
        await update.message.reply_photo(photo=f)


async def test_absences(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande de debug temporaire : /absences Real Madrid ; Barcelona ; 2026-08-20"""
    if not API_FOOTBALL_KEY:
        await update.message.reply_text("API_FOOTBALL_KEY n'est pas configurée sur le serveur.")
        return
    text = " ".join(context.args)
    parts = [p.strip() for p in text.split(";")]
    if len(parts) != 3:
        await update.message.reply_text("Utilisation : /absences Real Madrid ; Barcelona ; 2026-08-20")
        return
    home_name, away_name, match_date = parts
    absences = get_absences(home_name, away_name, match_date)
    if absences is None:
        await update.message.reply_text("Impossible de récupérer les données (équipe/date non trouvée, ou quota dépassé).")
    else:
        await update.message.reply_text(format_absences(absences))


async def testall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande de debug temporaire : teste toutes les compétitions d'un coup
    et résume, pour chacune, le nombre de matchs à venir trouvés et si
    l'Élo/les absences se résolvent pour le premier match."""
    await update.message.reply_text(f"🧪 Test de {len(COMPETITIONS)} compétitions en cours...")

    lines = []
    for code, name in COMPETITIONS.items():
        try:
            matches = get_upcoming_matches(code)
        except requests.HTTPError as e:
            lines.append(f"❌ {code} ({name}): erreur API ({e})")
            continue

        if not matches:
            lines.append(f"⚪ {code} ({name}): 0 match dans les {PREDICT_DAYS_AHEAD} prochains jours")
            continue

        first = matches[0]
        home_name = first["homeTeam"]["name"]
        away_name = first["awayTeam"]["name"]
        elo_home = get_team_elo(home_name)
        elo_status = f"Élo OK ({round(elo_home)})" if elo_home else "Élo non trouvé"

        absences_status = "N/A (clé non configurée)"
        if API_FOOTBALL_KEY:
            match_date = first["utcDate"][:10]
            absences = get_absences(home_name, away_name, match_date)
            absences_status = "Absences OK" if absences is not None else "Absences non trouvées"

        lines.append(
            f"✅ {code} ({name}): {len(matches)} match(s), ex: {home_name} vs {away_name}\n"
            f"    {elo_status} | {absences_status}"
        )

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


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


async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vérification manuelle immédiate : /live PL"""
    if not context.args:
        await update.message.reply_text("Utilisation : /live PL  (voir /ligues pour les codes)")
        return
    code = context.args[0].upper()
    if code not in COMPETITIONS:
        await update.message.reply_text("Code inconnu. Essaie /ligues pour voir la liste.")
        return

    try:
        matches = get_live_matches(code)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Erreur API : {e}")
        return

    if not matches:
        await update.message.reply_text(f"Aucun match en direct actuellement pour {COMPETITIONS[code]}.")
        return

    state = load_live_state()
    lines = [f"🔴 {len(matches)} match(s) en direct — {COMPETITIONS[code]} :\n"]
    any_alert = False
    for m in matches:
        score = m["score"]["fullTime"]
        home, away = m["homeTeam"]["name"], m["awayTeam"]["name"]
        minute = m.get("minute", "?")
        lines.append(f"• {home} {score.get('home', '?')}-{score.get('away', '?')} {away} (minute {minute})")
        alert = check_live_match_for_burst(m, state)
        if alert:
            any_alert = True
            lines.append(f"   ⚠️ Sursaut: {alert['goals_since_last']} but(s) rapprochés")
    save_live_state(state)

    if not any_alert:
        lines.append("\n(Aucun sursaut suspect détecté pour l'instant — cette vérification mémorise "
                      "aussi le score actuel pour comparer au prochain /live ou à la surveillance automatique.)")

    await update.message.reply_text("\n".join(lines))


async def live_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = load_live_subscribers()
    subs.add(chat_id)
    save_live_subscribers(subs)
    await update.message.reply_text(
        f"✅ Surveillance en direct activée. Le bot vérifie toutes les {LIVE_CHECK_INTERVAL_MINUTES} min "
        "et t'alerte en cas de sursaut de buts suspect sur un match en cours.\n"
        "⚠️ Rappel : les données peuvent être retardées de quelques minutes, ce n'est pas du temps réel garanti."
    )


async def live_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subs = load_live_subscribers()
    subs.discard(chat_id)
    save_live_subscribers(subs)
    await update.message.reply_text("❌ Surveillance en direct désactivée.")


async def odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vérification manuelle des cotes et de leur mouvement : /odds PL"""
    if not ODDS_API_KEY:
        await update.message.reply_text(
            "ODDS_API_KEY n'est pas configurée sur le serveur — cette fonctionnalité est désactivée."
        )
        return

    if not context.args:
        await update.message.reply_text("Utilisation : /odds PL  (voir /ligues pour les codes)")
        return

    code = context.args[0].upper()
    if code not in COMPETITIONS:
        await update.message.reply_text("Code inconnu. Essaie /ligues pour voir la liste.")
        return
    if code not in ODDS_SPORT_KEYS:
        await update.message.reply_text(f"Les cotes ne sont pas disponibles pour {COMPETITIONS[code]} actuellement.")
        return

    await update.message.reply_text(f"💰 Récupération des cotes pour {COMPETITIONS[code]}...")

    entries = check_odds_movement(code)
    if not entries:
        await update.message.reply_text(
            "Aucune cote disponible actuellement (pas de match à venir, ou quota The Odds API épuisé)."
        )
        return

    any_movement = any(e["movement"] for e in entries)
    lines = [f"💰 Cotes actuelles — {COMPETITIONS[code]} ({len(entries)} match(s)) :\n"]
    lines.append(format_odds_report(entries))
    if not any_movement:
        lines.append(
            "\n(Aucun mouvement notable détecté — soit c'est stable, soit c'est ta première "
            "vérification pour ces matchs, sans point de comparaison.)"
        )

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def oddsnow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Version simplifiée : juste les probabilités 1X2 actuelles, sans
    comparaison avec une vérification précédente. Utilisation : /oddsnow PL"""
    if not ODDS_API_KEY:
        await update.message.reply_text(
            "ODDS_API_KEY n'est pas configurée sur le serveur — cette fonctionnalité est désactivée."
        )
        return

    if not context.args:
        await update.message.reply_text("Utilisation : /oddsnow PL  (voir /ligues pour les codes)")
        return

    code = context.args[0].upper()
    if code not in COMPETITIONS:
        await update.message.reply_text("Code inconnu. Essaie /ligues pour voir la liste.")
        return
    sport_key = ODDS_SPORT_KEYS.get(code)
    if not sport_key:
        await update.message.reply_text(f"Les cotes ne sont pas disponibles pour {COMPETITIONS[code]} actuellement.")
        return

    await update.message.reply_text(f"💰 Récupération des cotes pour {COMPETITIONS[code]}...")

    events = get_current_odds(sport_key)
    if not events:
        await update.message.reply_text(
            "Aucune cote disponible actuellement (pas de match à venir, ou quota The Odds API épuisé)."
        )
        return

    lines = [f"💰 Probabilités 1X2 — {COMPETITIONS[code]} ({len(events)} match(s)) :\n"]
    for event in events:
        probs = implied_probabilities_h2h(event)
        if not probs:
            continue
        line = f"• {event.get('home_team')} vs {event.get('away_team')} — Dom {round(probs['home']*100)}%"
        if probs.get("draw") is not None:
            line += f" | Nul {round(probs['draw']*100)}%"
        line += f" | Ext {round(probs['away']*100)}%"
        lines.append(line)

    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


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
    app.add_handler(CommandHandler("predict", predict))
    app.add_handler(CommandHandler("elo", test_elo))
    app.add_handler(CommandHandler("elochart", elochart))
    app.add_handler(CommandHandler("absences", test_absences))
    app.add_handler(CommandHandler("testall", testall))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("live_subscribe", live_subscribe))
    app.add_handler(CommandHandler("live_unsubscribe", live_unsubscribe))
    app.add_handler(CommandHandler("odds", odds))
    app.add_handler(CommandHandler("oddsnow", oddsnow))

    app.job_queue.run_daily(send_daily_alert, time=dtime(hour=DAILY_ALERT_HOUR_UTC, minute=0))
    app.job_queue.run_repeating(check_live_burst_job, interval=LIVE_CHECK_INTERVAL_MINUTES * 60, first=30)

    logger.info("Bot démarré, en écoute...")
    app.run_polling()


if __name__ == "__main__":
    main()
