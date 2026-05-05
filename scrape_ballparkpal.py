#!/usr/bin/env python3
"""
BallparkPal Scraper
-------------------
Logs into ballparkpal.com, scrapes today's game simulations,
and writes data.json to the repo root for the dashboard to consume.

Usage:
    python scrape_ballparkpal.py

Environment variables (set in GitHub Actions secrets or .env):
    BPP_EMAIL
    BPP_PASSWORD

Output:
    data.json  (place this in the same folder as your dashboard HTML)
"""

import os
import json
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL   = "https://www.ballparkpal.com"
GAMES_URL  = f"{BASE_URL}/Game-Simulations.php"
OUTPUT     = os.path.join(os.path.dirname(__file__), "data.json")

PHPSESSID    = os.environ.get("BPP_PHPSESSID", "")
CF_CLEARANCE = os.environ.get("BPP_CF_CLEARANCE", "")

EASTERN = ZoneInfo("America/New_York")

# ── Helpers ───────────────────────────────────────────────────────────────────
def clean(text):
    return " ".join(text.split()) if text else ""

def pct_to_float(text):
    """'62.3%' → 62.3"""
    try:
        return float(text.strip().rstrip("%"))
    except Exception:
        return None

def runs_val(text):
    try:
        return float(text.strip())
    except Exception:
        return None

def park_factor_class(val):
    """Return 'pos', 'neg', or 'neut' based on park factor value string like '+18% Runs'"""
    if not val:
        return "neut"
    m = re.search(r'([+-]?\d+)', val)
    if not m:
        return "neut"
    n = int(m.group(1))
    if n > 2:
        return "pos"
    if n < -2:
        return "neg"
    return "neut"

# ── Auth ──────────────────────────────────────────────────────────────────────
def apply_cookies(session):
    session.cookies.set("PHPSESSID",    PHPSESSID,    domain="www.ballparkpal.com")
    session.cookies.set("cf_clearance", CF_CLEARANCE, domain="www.ballparkpal.com")
    print("✓ Session cookies applied")

# ── Scrape game list ──────────────────────────────────────────────────────────
def scrape_games(session):
    r = session.get(GAMES_URL, timeout=20)
    if r.status_code != 200:
        print(f"ERROR: Could not load {GAMES_URL} (status {r.status_code})")
        sys.exit(1)

    soup = BeautifulSoup(r.text, "html.parser")
    containers = soup.select("div.summaryDescriptionContainer")

    if not containers:
        print("⚠ No game containers found — dumping page for debugging.")
        with open("debug_games_page.html", "w") as f:
            f.write(r.text)
        return []

    games = []
    for container in containers:
        try:
            anchor = container.find_previous_sibling("a")
            gamepk = ""
            if anchor and anchor.get("id", "").startswith("game_"):
                gamepk = anchor["id"].replace("game_", "")
            game = parse_game_container(container, gamepk)
            if game:
                games.append(game)
        except Exception as e:
            print(f"  Skipping game due to error: {e}")

    return games

# ── Parse one game container ──────────────────────────────────────────────────
def parse_game_container(container, gamepk):
    g = {"gamepk": gamepk}

    # Time — first .atSymbol holds the game time
    time_el = container.select_one(".atSymbol")
    g["time"] = clean(time_el.get_text()) if time_el else ""

    # Team names — awayTeam/homeTeam divs that have a color style (not the logo row)
    away_name_el = container.select_one(".awayTeam[style*='color']")
    home_name_el = container.select_one(".homeTeam[style*='color']")
    g["away"] = clean(away_name_el.get_text()) if away_name_el else "Away"
    g["home"] = clean(home_name_el.get_text()) if home_name_el else "Home"

    # Starters — awayTeam/homeTeam divs containing a Pitcher-Summary link
    away_sp = container.select_one(".awayTeam a[href*='Pitcher-Summary']")
    home_sp = container.select_one(".homeTeam a[href*='Pitcher-Summary']")
    g["away_starter"] = clean(away_sp.get_text()) if away_sp else ""
    g["home_starter"] = clean(home_sp.get_text()) if home_sp else ""

    # Park and park factor — middleText div containing a Park-Summary link
    park_link = container.find("a", href=re.compile(r"Park-Summary"))
    if park_link:
        park_row_text = clean(park_link.parent.get_text())  # "Coors Field | Runs: +28%"
        g["park"] = clean(park_link.get_text())
        pf_m = re.search(r'Runs:\s*([+-]?\d+)%', park_row_text)
        g["park_factor"] = f"{pf_m.group(1)}%" if pf_m else ""
    else:
        g["park"] = ""
        g["park_factor"] = ""
    g["park_factor_class"] = park_factor_class(g["park_factor"])

    # YRFI — "YRFI: 56.1% (-128)"
    yrfi_el = container.select_one(".yrfi")
    if yrfi_el:
        m = re.search(r'YRFI:\s*([\d.]+)%', yrfi_el.get_text())
        g["yrfi"] = float(m.group(1)) if m else None
    else:
        g["yrfi"] = None

    # Build a label→(away_text, home_text) map from labeled rows.
    # Each row is: [float-left away div] [float-left label div] [float-left home div]
    row_map = {}
    for mid_div in container.select(".middleText"):
        label = clean(mid_div.get_text())
        parent = mid_div.parent
        prev_sib = parent.find_previous_sibling("div")
        next_sib = parent.find_next_sibling("div")
        away_el = prev_sib.select_one(".awayTeam") if prev_sib else None
        home_el = next_sib.select_one(".homeTeam") if next_sib else None
        row_map[label] = (
            clean(away_el.get_text()) if away_el else "",
            clean(home_el.get_text()) if home_el else "",
        )

    # Projected runs — label "Runs"
    runs = row_map.get("Runs", ("", ""))
    g["away_proj"] = runs_val(runs[0])
    g["home_proj"] = runs_val(runs[1])
    g["model_total"] = round(g["away_proj"] + g["home_proj"], 2) if g["away_proj"] and g["home_proj"] else None

    # Win probabilities — label "Win", format "(+115) 46.6%" / "53.4% (-115)"
    win = row_map.get("Win", ("", ""))
    away_w = re.search(r'([\d.]+)%', win[0])
    home_w = re.search(r'([\d.]+)%', win[1])
    g["away_win_pct"] = float(away_w.group(1)) if away_w else None
    g["home_win_pct"] = float(home_w.group(1)) if home_w else None

    # ML odds — label "ML", format "-135 (BetMGM)" / "+120 (BetRivers)"
    ml = row_map.get("ML", ("", ""))
    away_ml = re.search(r'([+-]?\d+)', ml[0])
    home_ml = re.search(r'([+-]?\d+)', ml[1])
    g["away_ml"] = int(away_ml.group(1)) if away_ml else None
    g["home_ml"] = int(home_ml.group(1)) if home_ml else None

    # O/U line — label "Total", home side has "Over 10.5, -112 (Novig)"
    total = row_map.get("Total", ("", ""))
    ou_m = re.search(r'([\d.]+)', total[1])
    g["ou_line"] = float(ou_m.group(1)) if ou_m else None

    g["best_bet"] = False
    return g

# ── Identify best bets ────────────────────────────────────────────────────────
def identify_best_bets(games):
    """
    Simple best-bet logic mirroring the original dashboard:
    - Flag games where model_total is significantly above ou_line (overs)
    - Flag games where model win% differs significantly from implied ML odds
    Best bets are sorted by edge size; top 3 get flagged.
    """
    candidates = []
    for g in games:
        edge = None
        bet_type = None
        bet_desc = ""

        # Over edge
        if g.get("model_total") and g.get("ou_line"):
            over_edge = g["model_total"] - g["ou_line"]
            if over_edge > 1.5:
                edge = over_edge
                bet_type = "over"
                bet_desc = f"OVER {g['away']} @ {g['home']} {g['ou_line']}"

        # ML edge (home underdog)
        if g.get("home_win_pct") and g.get("home_ml"):
            implied = ml_to_prob(g["home_ml"])
            if implied:
                ml_edge = g["home_win_pct"] - implied * 100
                if ml_edge > 10 and (edge is None or ml_edge > edge):
                    edge = ml_edge
                    bet_type = "ml_home"
                    bet_desc = f"{g['home']} +{g['home_ml']}"

        if edge and bet_type:
            candidates.append((edge, bet_type, bet_desc, g))

    candidates.sort(key=lambda x: x[0], reverse=True)

    for i, (edge, bet_type, bet_desc, g) in enumerate(candidates[:3]):
        g["best_bet"] = True
        g["best_bet_rank"] = i + 1
        g["best_bet_type"] = bet_type
        g["best_bet_desc"] = bet_desc
        g["best_bet_edge"] = round(edge, 2)

def ml_to_prob(ml):
    """American odds → implied probability (0–1)"""
    try:
        ml = float(ml)
        if ml > 0:
            return 100 / (ml + 100)
        else:
            return abs(ml) / (abs(ml) + 100)
    except Exception:
        return None

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    })

    apply_cookies(session)

    print(f"Scraping {GAMES_URL} ...")
    games = scrape_games(session)
    print(f"  Found {len(games)} games")

    if games:
        identify_best_bets(games)

    now_et = datetime.now(EASTERN)
    today  = now_et.strftime("%B %-d, %Y")   # "May 4, 2026"
    now_ts = now_et.strftime("%-I:%M %p ET")  # "9:27 AM ET"

    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "scraped_at_display": f"Scraped {today} · {now_ts}",
        "date_label": today,
        "games": games,
        "best_bets": [g for g in games if g.get("best_bet")],
    }

    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)

    print(f"✓ Wrote {OUTPUT}  ({len(games)} games, {len(output['best_bets'])} best bets)")

if __name__ == "__main__":
    main()
