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
  python3 update_live_data.py --dry-run          # show changes, don't write
  python3 update_live_data.py --quiet            # minimal output (for cron)
  python3 update_live_data.py --push             # auto-commit + push to GitHub after update

Dependencies (one-time):
  pip3 install requests beautifulsoup4
"""
import json, sys, argparse, datetime, subprocess
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

USER_AGENT = "WC2026Tool/1.1 (data refresh script; contact: action-network)"
TIMEOUT = 20

# Map external names (Wikipedia / FIFA / Elo) → our internal team_meta keys
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
    p.add_argument("--push", action="store_true", help="After update, git add + commit + push live_data.json")
    args = p.parse_args()
    if not args.quiet:
        print(f"=== WC 2026 live_data updater · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    update_live_data(args)
    if not args.quiet: print("\nDone.")

if __name__ == "__main__":
    main()
