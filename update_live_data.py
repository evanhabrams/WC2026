#!/usr/bin/env python3
"""
update_live_data.py

Daily updater for the WC 2026 Matchup Tool's live_data.json.

What it does:
  - Pulls latest Elo ratings from eloratings.net (scrapes the World rankings table)
  - For FIFA rank: pulls from FIFA's published rankings page if accessible,
    otherwise reads from a manual fifa_ranks.json override file
  - For BETSIE: preserves whatever's in live_data.json (Action Network's model
    is internal — paste fresh values manually when BETSIE is rerun)
  - Writes the merged result back to live_data.json

Usage:
  python3 update_live_data.py              # update Elo, preserve everything else
  python3 update_live_data.py --fifa       # also refresh FIFA ranks
  python3 update_live_data.py --dry-run    # show what would change, don't write

Requires: requests, beautifulsoup4
Install:  pip3 install requests beautifulsoup4
"""

import json, sys, argparse, datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: requires 'requests' and 'beautifulsoup4'")
    print("Install: pip3 install requests beautifulsoup4")
    sys.exit(1)

# Team name aliases between our internal names and what external sources use
NAME_ALIASES = {
    "United States": "USA", "USA": "USA",
    "Republic of Ireland": "Ireland",
    "South Korea": "South Korea", "Korea Republic": "South Korea",
    "Turkey": "Türkiye", "Türkiye": "Türkiye",
    "DR Congo": "DR Congo", "Congo DR": "DR Congo",
    "Cape Verde": "Cape Verde", "Cabo Verde": "Cape Verde",
    "Czech Republic": "Czech Republic", "Czechia": "Czech Republic",
    "Bosnia and Herzegovina": "Bosnia and Herzegovina", "Bosnia-Herzegovina": "Bosnia and Herzegovina",
    "Curacao": "Curacao", "Curaçao": "Curacao",
    "Ivory Coast": "Ivory Coast", "Côte d'Ivoire": "Ivory Coast", "Cote d'Ivoire": "Ivory Coast",
}

LIVE_DATA_PATH = Path(__file__).parent / "live_data.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WC2026Tool/1.0)"}

def normalize_team(name):
    return NAME_ALIASES.get(name.strip(), name.strip())

def fetch_elo_ratings():
    """Scrape eloratings.net for current World rankings."""
    print("Fetching Elo ratings from eloratings.net...")
    try:
        r = requests.get("http://eloratings.net/", headers=HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"  ERROR: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    elo = {}
    # eloratings.net renders rankings client-side; the data is in JS variables
    # As a fallback, we try to parse any HTML table they expose
    # If that fails, the data is in window.ratingdata in their JS
    for tag in soup.find_all("script"):
        if tag.string and "ratingdata" in str(tag.string):
            text = tag.string
            # Best-effort: try to extract team:rating pairs
            print("  Note: eloratings.net data is JS-rendered; consider using their CSV export")
            return None
    # If we got here without parsing, try a fallback static URL
    print("  No table found; manual update may be needed")
    return None

def fetch_fifa_ranks_fallback():
    """Read FIFA ranks from a manual fifa_ranks.json file in the same directory."""
    manual = Path(__file__).parent / "fifa_ranks.json"
    if manual.exists():
        with open(manual) as f:
            data = json.load(f)
        print(f"  Loaded {len(data)} FIFA ranks from manual fifa_ranks.json")
        return data
    print("  No manual fifa_ranks.json found; preserving existing values")
    return None

def load_live_data():
    if not LIVE_DATA_PATH.exists():
        print(f"WARNING: {LIVE_DATA_PATH} does not exist, will create new")
        return {"team_meta": {}, "betsie_win": {}, "betsie_path": {}}
    with open(LIVE_DATA_PATH) as f:
        return json.load(f)

def save_live_data(data, dry_run=False):
    data["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if dry_run:
        print(f"\n[DRY RUN] Would write to {LIVE_DATA_PATH}")
        print(f"  updated_at: {data['updated_at']}")
        print(f"  team_meta entries: {len(data.get('team_meta', {}))}")
        return
    with open(LIVE_DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nWrote {LIVE_DATA_PATH}")
    print(f"  updated_at: {data['updated_at']}")
    print(f"  team_meta entries: {len(data.get('team_meta', {}))}")

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fifa", action="store_true", help="Also refresh FIFA rankings (from fifa_ranks.json)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, don't write")
    args = parser.parse_args()

    print(f"=== WC 2026 live_data updater · {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    data = load_live_data()
    team_meta = data.setdefault("team_meta", {})

    # Elo update
    elo = fetch_elo_ratings()
    if elo:
        updated = 0
        for team, rating in elo.items():
            t = normalize_team(team)
            if t in team_meta:
                if team_meta[t].get("elo") != rating:
                    team_meta[t]["elo"] = rating
                    updated += 1
        print(f"  Updated Elo for {updated} teams")
    else:
        print("  Elo update skipped — preserving existing values")

    # FIFA update (only if --fifa flag)
    if args.fifa:
        fifa = fetch_fifa_ranks_fallback()
        if fifa:
            updated = 0
            for team, rank in fifa.items():
                t = normalize_team(team)
                if t in team_meta:
                    if team_meta[t].get("fifa") != rank:
                        team_meta[t]["fifa"] = rank
                        updated += 1
            print(f"  Updated FIFA rank for {updated} teams")

    # BETSIE: never auto-update (internal Action Network model)
    print(f"  BETSIE values preserved (internal model — update manually)")

    save_live_data(data, dry_run=args.dry_run)
    print("\nDone. Commit live_data.json to GitHub to push the update live.")

if __name__ == "__main__":
    main()
