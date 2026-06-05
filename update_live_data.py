#!/usr/bin/env python3
"""
update_live_data.py — Daily refresher for the WC 2026 Matchup Tool's live_data.json

What it does, in priority order:
  FIFA ranks:
    1. Scrape Wikipedia 'FIFA Men's World Ranking' top-20 table (fresh, updates within hours of FIFA release)
    2. Fill remaining 28 of 48 WC teams from fifa_ranks.json (manual override file, baseline April 1 2026)
  Elo ratings:
    1. Scrape Wikipedia 'World Football Elo Ratings' top-20 table (updates periodically)
    2. Fill remaining 28 of 48 WC teams from elo_ratings.json (manual override file, baseline Jan 19 2026)
  BETSIE win + path:
    Always preserve existing values (internal Action Network model, paste manually when refreshed)

Usage:
  python3 update_live_data.py                    # full refresh (default — try web, fall back to JSON files)
  python3 update_live_data.py --offline          # skip web, use JSON files only (use when offline or rate-limited)
  python3 update_live_data.py --offline-fifa     # web Elo, file FIFA (use after manually editing fifa_ranks.json)
  python3 update_live_data.py --offline-elo      # web FIFA, file Elo
  python3 update_live_data.py --odds             # ALSO fetch + cache odds from The Odds API (uses 2 requests)
  python3 update_live_data.py --giffen           # ALSO fetch Giffen xG projections from Google Sheets
  python3 update_live_data.py --dry-run          # show changes, don't write
  python3 update_live_data.py --quiet            # minimal output (for cron)
  python3 update_live_data.py --push             # auto-commit + push to GitHub after update

  Typical daily run:  python3 update_live_data.py --odds --giffen --push

Dependencies (one-time):
  pip3 install requests beautifulsoup4
  pip3 install google-auth google-api-python-client    # for --giffen

For --giffen: requires google_service_account.json in the same folder (gitignored).
The sheet must be shared (Viewer) with the service account email inside that JSON.

Dependencies (one-time):
  pip3 install requests beautifulsoup4
"""
import json, sys, os, argparse, datetime, subprocess
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: missing dependencies. Install with:")
    print("  pip3 install requests beautifulsoup4")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent
LIVE_DATA = SCRIPT_DIR / "live_data.json"
FIFA_FILE = SCRIPT_DIR / "fifa_ranks.json"
ELO_FILE = SCRIPT_DIR / "elo_ratings.json"

USER_AGENT = "WC2026Tool/1.2 (data refresh script; contact: action-network)"
TIMEOUT = 20

# Odds API key is read from the environment so it never lives in the repo.
# Set it once in your shell:  export ODDS_API_KEY="your_new_key"
# (add that line to ~/.zshrc to make it permanent). For GitHub Actions, add it
# as a repo secret and expose it via `env:` in the workflow.
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"

# Giffen xG projections (Google Sheets, fetched via service account)
GIFFEN_SHEET_ID = "12v8WebV5SakStof9ByCLxAKZWeePqbBgGyZs1dTh_DE"
GIFFEN_TAB_NAME = "Per Game xG"
GIFFEN_RANGE = f"'{GIFFEN_TAB_NAME}'!A1:G300"  # A-G columns; max 300 rows handles full bracket
GIFFEN_CREDENTIALS_FILE = "google_service_account.json"

# Map external names (Wikipedia / FIFA / Elo / Giffen sheet) → our internal team_meta keys
NAME_ALIASES = {
    "United States": "USA", "USA": "USA", "US": "USA",
    "Korea Republic": "South Korea", "South Korea": "South Korea",
    "Türkiye": "Turkey", "Turkey": "Turkey",
    "Czechia": "Czech Republic", "Czech Republic": "Czech Republic",
    "DR Congo": "DR Congo", "Congo DR": "DR Congo", "Democratic Republic of the Congo": "DR Congo",
    "Cape Verde": "Cape Verde", "Cabo Verde": "Cape Verde",
    "Curaçao": "Curacao", "Curacao": "Curacao",
    "Ivory Coast": "Ivory Coast", "Côte d'Ivoire": "Ivory Coast", "Cote d'Ivoire": "Ivory Coast",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina", "Bosnia & Herzegovina": "Bosnia and Herzegovina", "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Republic of Ireland": "Ireland", "Ireland": "Ireland",
}

def normalize(name):
    return NAME_ALIASES.get(name.strip(), name.strip())

# ============================================================
# Web scrapers (best-effort; gracefully fall back to JSON files)
# ============================================================

def scrape_fifa_top20():
    """Wikipedia's 'FIFA Men's World Ranking' page has a top-20 table near the top."""
    url = "https://en.wikipedia.org/wiki/FIFA_Men%27s_World_Ranking"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [web] FIFA scrape failed: {type(e).__name__}: {e}")
        return None, None
    soup = BeautifulSoup(r.text, "html.parser")
    # Find the first wikitable with column header "Team" and "Rank"
    for table in soup.find_all("table", class_="wikitable"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")[:6]]
        if "Team" in headers and ("Rank" in headers or "#" in headers):
            ranks = {}
            for row in table.find_all("tr")[1:25]:
                cells = row.find_all(["td", "th"])
                if len(cells) < 3: continue
                try:
                    rank = int(cells[0].get_text(strip=True))
                    # Team name often in col 2 or 3 (col 2 = change icon, sometimes empty)
                    team_text = cells[2].get_text(strip=True) or cells[1].get_text(strip=True)
                    # Strip any [reference] markers
                    team = team_text.split('[')[0].strip()
                    if team:
                        ranks[normalize(team)] = rank
                except (ValueError, IndexError):
                    continue
            if ranks:
                date_caption = soup.find(string=lambda s: s and "Top 20 rankings as of" in s)
                date_str = str(date_caption).replace("Top 20 rankings as of", "").strip() if date_caption else "unknown"
                return ranks, date_str
    print("  [web] FIFA: no wikitable matched expected schema")
    return None, None

def scrape_elo_top20():
    """Wikipedia's 'World Football Elo Ratings' page has a top-20 table."""
    url = "https://en.wikipedia.org/wiki/World_Football_Elo_Ratings"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  [web] Elo scrape failed: {type(e).__name__}: {e}")
        return None, None
    soup = BeautifulSoup(r.text, "html.parser")
    for table in soup.find_all("table", class_="wikitable"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")[:6]]
        if "Team" in headers and ("Points" in headers or "Rating" in headers):
            ratings = {}
            for row in table.find_all("tr")[1:25]:
                cells = row.find_all(["td", "th"])
                if len(cells) < 4: continue
                try:
                    team_text = cells[2].get_text(strip=True) or cells[1].get_text(strip=True)
                    team = team_text.split('[')[0].strip()
                    points = int(cells[3].get_text(strip=True).replace(",", ""))
                    if team:
                        ratings[normalize(team)] = points
                except (ValueError, IndexError):
                    continue
            if ratings:
                date_caption = soup.find(string=lambda s: s and "Top 20 rankings as of" in s)
                date_str = str(date_caption).replace("Top 20 rankings as of", "").strip() if date_caption else "unknown"
                return ratings, date_str
    print("  [web] Elo: no wikitable matched expected schema")
    return None, None

# ============================================================
# Odds API (The Odds API — soccer_fifa_world_cup + soccer_fifa_world_cup_winner)
# ============================================================

def american_to_implied(price):
    """Convert American odds to implied probability (with vig). Returns 0-1."""
    if price is None:
        return None
    if price > 0:
        return 100.0 / (price + 100)
    else:
        return abs(price) / (abs(price) + 100)

def fetch_match_odds():
    """Fetch H2H + spreads + totals for all WC events. Returns list of normalized events."""
    if not ODDS_API_KEY:
        print("  [odds] skipped: ODDS_API_KEY not set. Run: export ODDS_API_KEY=\"your_key\"")
        return None
    url = f"{ODDS_BASE}/sports/soccer_fifa_world_cup/odds/"
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american"}
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        events = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"  [odds] match odds: {len(events)} events fetched (quota remaining: {remaining})")
    except requests.RequestException as e:
        print(f"  [odds] match odds fetch failed: {type(e).__name__}: {e}")
        return None

    normalized = []
    for ev in events:
        out = {
            "id": ev.get("id"),
            "commence": ev.get("commence_time"),
            "home": normalize(ev.get("home_team", "")),
            "away": normalize(ev.get("away_team", "")),
            "books": [],
            "consensus": {"h2h": {}, "totals": {}, "spreads": {}},
        }
        h2h_b = {"home": [], "away": [], "draw": []}
        totals_b = {}
        spreads_b = {}
        for bm in ev.get("bookmakers", []):
            out["books"].append(bm.get("title"))
            for m in bm.get("markets", []):
                key = m.get("key")
                for o in m.get("outcomes", []):
                    name = o.get("name", "")
                    price = o.get("price")
                    point = o.get("point")
                    if key == "h2h":
                        if name == out["home"]: h2h_b["home"].append(price)
                        elif name == out["away"]: h2h_b["away"].append(price)
                        elif name.lower() == "draw": h2h_b["draw"].append(price)
                    elif key == "totals":
                        p = str(point)
                        totals_b.setdefault(p, {"over": [], "under": []})
                        if name.lower() == "over": totals_b[p]["over"].append(price)
                        elif name.lower() == "under": totals_b[p]["under"].append(price)
                    elif key == "spreads":
                        spreads_b.setdefault(str(point), {}).setdefault(name, []).append(price)
        def avg(lst): return round(sum(lst) / len(lst)) if lst else None
        out["consensus"]["h2h"] = {k: avg(v) for k, v in h2h_b.items()}
        out["consensus"]["totals"] = {p: {"over": avg(d["over"]), "under": avg(d["under"])} for p, d in totals_b.items()}
        out["consensus"]["spreads"] = {p: {n: avg(prs) for n, prs in teams.items()} for p, teams in spreads_b.items()}
        out["books"] = list(set(out["books"]))
        normalized.append(out)
    return normalized

def fetch_winner_outrights():
    """Fetch tournament winner futures."""
    if not ODDS_API_KEY:
        print("  [odds] skipped: ODDS_API_KEY not set. Run: export ODDS_API_KEY=\"your_key\"")
        return None
    url = f"{ODDS_BASE}/sports/soccer_fifa_world_cup_winner/odds/"
    params = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "outrights", "oddsFormat": "american"}
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        events = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"  [odds] outrights: {len(events)} events fetched (quota remaining: {remaining})")
    except requests.RequestException as e:
        print(f"  [odds] outrights fetch failed: {type(e).__name__}: {e}")
        return None

    if not events:
        return None
    teams_b = {}
    books = []
    for ev in events:
        for bm in ev.get("bookmakers", []):
            books.append(bm.get("title"))
            for m in bm.get("markets", []):
                for o in m.get("outcomes", []):
                    teams_b.setdefault(normalize(o.get("name", "")), []).append(o.get("price"))
    consensus = {t: round(sum(p) / len(p)) for t, p in teams_b.items() if p}
    return {"consensus": consensus, "books": sorted(set(books))}

# ============================================================
# Giffen xG Projections (Google Sheets via service account)
# ============================================================

def fetch_giffen_projections():
    """Read Giffen's 'Per Game xG' tab from the shared Google Sheet.
    Returns a list of {home, away, xg_h, xg_a, tot_xg, h_spr, game_no} dicts.
    Returns None if credentials missing or fetch fails (caller falls back gracefully)."""
    cred_path = SCRIPT_DIR / GIFFEN_CREDENTIALS_FILE
    if not cred_path.exists():
        print(f"  [giffen] credentials file missing: {GIFFEN_CREDENTIALS_FILE}")
        print(f"           Place service account JSON in {SCRIPT_DIR} (must be gitignored)")
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        print("  [giffen] missing dependencies. Install with:")
        print("           pip3 install google-auth google-api-python-client")
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            str(cred_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = service.spreadsheets().values().get(
            spreadsheetId=GIFFEN_SHEET_ID,
            range=GIFFEN_RANGE,
        ).execute()
    except Exception as e:
        msg = str(e)[:200]
        print(f"  [giffen] API error: {type(e).__name__}: {msg}")
        if "403" in msg or "PERMISSION_DENIED" in msg:
            print("           Sheet may not be shared with the service account email.")
            print(f"           Service account email is in {GIFFEN_CREDENTIALS_FILE} (client_email field).")
        return None

    rows = result.get("values", [])
    if len(rows) < 2:
        print(f"  [giffen] sheet returned {len(rows)} rows (expected header + data)")
        return None

    # Skip header row, parse data
    header = rows[0]
    expected = ["Game No.", "Home", "Away", "xG H", "xG A", "tot xG", "H Spr"]
    if header[:7] != expected:
        print(f"  [giffen] header mismatch — got {header[:7]}")
        print(f"           expected {expected}")
        # Continue anyway; column positions are fixed

    projections = []
    unmapped = set()
    for r in rows[1:]:
        if len(r) < 5: continue  # incomplete row
        try:
            game_no = int(r[0]) if r[0] else None
            home_raw = r[1].strip() if len(r) > 1 else ""
            away_raw = r[2].strip() if len(r) > 2 else ""
            xg_h = float(r[3]) if len(r) > 3 and r[3] else None
            xg_a = float(r[4]) if len(r) > 4 and r[4] else None
            tot_xg = float(r[5]) if len(r) > 5 and r[5] else None
            h_spr = float(r[6]) if len(r) > 6 and r[6] else None
        except (ValueError, IndexError):
            continue  # skip malformed row

        if not home_raw or not away_raw or xg_h is None or xg_a is None:
            continue

        home = normalize(home_raw)
        away = normalize(away_raw)

        # Track unmapped names (potential alias gaps)
        # (We don't have team_meta here to check against — caller will warn)
        if home_raw != home: pass  # mapped via alias
        if away_raw != away: pass  # mapped via alias

        projections.append({
            "game_no": game_no,
            "home": home,
            "away": away,
            "home_raw": home_raw if home != home_raw else None,
            "away_raw": away_raw if away != away_raw else None,
            "xg_h": xg_h,
            "xg_a": xg_a,
            "tot_xg": tot_xg if tot_xg is not None else round(xg_h + xg_a, 3),
            "h_spr": h_spr if h_spr is not None else round(xg_a - xg_h, 3),  # note: Giffen formula is xG_A - xG_H
        })

    return projections


# ============================================================
# JSON fallback loaders
# ============================================================

def load_json_file(path, label):
    if not path.exists():
        print(f"  [file] {label}: {path.name} not found")
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  [file] {label}: invalid JSON in {path.name}: {e}")
        return None, None
    meta = data.pop("_meta", {})
    as_of = meta.get("as_of", "unknown")
    print(f"  [file] {label}: {len(data)} entries from {path.name} (as of {as_of})")
    return {normalize(k): v for k, v in data.items()}, as_of

# ============================================================
# Core update loop
# ============================================================

def update_live_data(args):
    if not LIVE_DATA.exists():
        print(f"ERROR: {LIVE_DATA} not found. Run from deploy_package directory.")
        sys.exit(1)
    with open(LIVE_DATA) as f:
        data = json.load(f)
    team_meta = data.setdefault("team_meta", {})
    sources = data.setdefault("data_sources", {})

    # ----- FIFA -----
    fifa_ranks, fifa_date = (None, None)
    if not args.offline and not args.offline_fifa:
        if not args.quiet: print("\n=== FIFA RANKS ===")
        fifa_ranks, fifa_date = scrape_fifa_top20()
        if fifa_ranks and not args.quiet:
            print(f"  [web] {len(fifa_ranks)} teams (as of {fifa_date})")
    if not fifa_ranks:  # fall back to JSON
        if not args.quiet: print("\n=== FIFA RANKS (file fallback) ===")
        fifa_ranks, fifa_date = load_json_file(FIFA_FILE, "FIFA")
    if fifa_ranks:
        changes = []
        for team, rank in fifa_ranks.items():
            if team in team_meta and team_meta[team].get("fifa") != rank:
                changes.append((team, team_meta[team].get("fifa"), rank))
                team_meta[team]["fifa"] = rank
        if not args.quiet:
            print(f"  Applied {len(changes)} FIFA rank changes")
            for t, old, new in sorted(changes, key=lambda x: x[2])[:10]:
                print(f"    {t:30} #{old} → #{new}")
            if len(changes) > 10: print(f"    ... and {len(changes)-10} more")
        sources["fifa_rank"] = {"as_of": fifa_date, "method": "web" if not (args.offline or args.offline_fifa) else "file"}

    # ----- Elo -----
    elo_ratings, elo_date = (None, None)
    if not args.offline and not args.offline_elo:
        if not args.quiet: print("\n=== ELO RATINGS ===")
        elo_ratings, elo_date = scrape_elo_top20()
        if elo_ratings and not args.quiet:
            print(f"  [web] {len(elo_ratings)} teams (as of {elo_date})")
    if not elo_ratings:
        if not args.quiet: print("\n=== ELO RATINGS (file fallback) ===")
        elo_ratings, elo_date = load_json_file(ELO_FILE, "Elo")
    if elo_ratings:
        changes = []
        for team, rating in elo_ratings.items():
            if team in team_meta and team_meta[team].get("elo") != rating:
                changes.append((team, team_meta[team].get("elo"), rating))
                team_meta[team]["elo"] = rating
        if not args.quiet:
            print(f"  Applied {len(changes)} Elo changes")
            for t, old, new in sorted(changes, key=lambda x: -abs(x[2]-(x[1] or 0)))[:10]:
                print(f"    {t:30} {old} → {rating if False else new} ({(new - (old or 0)):+})")
            if len(changes) > 10: print(f"    ... and {len(changes)-10} more")
        sources["elo_rating"] = {"as_of": elo_date, "method": "web" if not (args.offline or args.offline_elo) else "file"}

    # ----- BETSIE: never auto-updated -----
    if not args.quiet: print("\n=== BETSIE ===\n  Preserved (internal model — paste new values into live_data.json manually)")

    # ----- ODDS API: opt-in via --odds flag -----
    if args.odds:
        if not args.quiet: print("\n=== ODDS API ===")
        matches = fetch_match_odds()
        if matches is not None:
            data["match_odds"] = matches
            sources["match_odds"] = {"as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                     "source": "The Odds API", "events": len(matches)}
            if not args.quiet:
                print(f"  Cached {len(matches)} match odds events")
                if matches:
                    sample = matches[0]
                    print(f"  Sample: {sample['home']} vs {sample['away']} ({sample.get('commence','')[:10]})")
                    print(f"    h2h consensus: {sample['consensus'].get('h2h')}")
        winners = fetch_winner_outrights()
        if winners is not None:
            data["winner_outrights"] = winners
            sources["winner_outrights"] = {"as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                           "source": "The Odds API", "books": winners.get("books", [])}
            if not args.quiet:
                top5 = sorted(winners["consensus"].items(), key=lambda x: x[1])[:5]
                print(f"  Cached {len(winners['consensus'])} team outrights from {len(winners['books'])} books")
                print(f"  Top 5 favorites: {', '.join(f'{t} +{p}' for t, p in top5)}")

    # ----- GIFFEN PROJECTIONS: opt-in via --giffen (also runs with --odds for daily convenience) -----
    if args.giffen or args.odds:
        if not args.quiet: print("\n=== GIFFEN xG PROJECTIONS ===")
        projections = fetch_giffen_projections()
        if projections is not None:
            data["match_projections"] = projections
            sources["match_projections"] = {
                "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "Action Network / Nick Giffen",
                "sheet": f"https://docs.google.com/spreadsheets/d/{GIFFEN_SHEET_ID}",
                "tab": GIFFEN_TAB_NAME,
                "fixtures": len(projections),
            }
            if not args.quiet:
                print(f"  Cached {len(projections)} match projections")
                if projections:
                    sample = projections[0]
                    print(f"  Sample: Game {sample.get('game_no','?')} | {sample['home']} {sample['xg_h']} xG vs {sample['away']} {sample['xg_a']} xG (tot {sample['tot_xg']})")
                # Surface any alias gaps for user awareness
                renamed = [p for p in projections if p.get("home_raw") or p.get("away_raw")]
                if renamed:
                    print(f"  Team-name aliases applied to {len(renamed)} fixtures (Giffen → tool keys):")
                    seen = set()
                    for p in renamed:
                        if p.get("home_raw") and p["home_raw"] not in seen:
                            print(f"    '{p['home_raw']}' → '{p['home']}'")
                            seen.add(p["home_raw"])
                        if p.get("away_raw") and p["away_raw"] not in seen:
                            print(f"    '{p['away_raw']}' → '{p['away']}'")
                            seen.add(p["away_raw"])

    # Bump timestamp
    data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if args.dry_run:
        print(f"\n[DRY RUN] Would write {LIVE_DATA} with updated_at = {data['updated_at']}")
        return
    with open(LIVE_DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if not args.quiet:
        print(f"\nWrote {LIVE_DATA}")
        print(f"  updated_at: {data['updated_at']}")

    # Optional auto-push to GitHub
    if args.push:
        if not args.quiet: print("\n=== GIT PUSH ===")
        try:
            subprocess.run(["git", "add", LIVE_DATA.name], cwd=SCRIPT_DIR, check=True)
            msg = f"Auto-refresh FIFA + Elo · {datetime.date.today().isoformat()}"
            subprocess.run(["git", "commit", "-m", msg], cwd=SCRIPT_DIR, check=True)
            subprocess.run(["git", "push"], cwd=SCRIPT_DIR, check=True)
            if not args.quiet: print(f"  Pushed: {msg}")
        except subprocess.CalledProcessError as e:
            print(f"  Git push failed (continuing): {e}")

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true", help="Skip web; use both JSON files only")
    p.add_argument("--offline-fifa", action="store_true", help="Use fifa_ranks.json only (skip Wikipedia for FIFA)")
    p.add_argument("--offline-elo", action="store_true", help="Use elo_ratings.json only (skip Wikipedia for Elo)")
    p.add_argument("--dry-run", action="store_true", help="Show changes, don't write")
    p.add_argument("--quiet", action="store_true", help="Minimal output (for cron jobs)")
    p.add_argument("--odds", action="store_true", help="Also fetch + cache odds from The Odds API (uses 2 requests/run)")
    p.add_argument("--giffen", action="store_true", help="Also fetch Giffen xG projections from Google Sheets (auto-enabled with --odds)")
    p.add_argument("--push", action="store_true", help="After update, git add + commit + push live_data.json")
    args = p.parse_args()
    if not args.quiet:
        print(f"=== WC 2026 live_data updater · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    update_live_data(args)
    if not args.quiet: print("\nDone.")

if __name__ == "__main__":
    main()
