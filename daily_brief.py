#!/usr/bin/env python3
"""
WC 2026 Daily SEO Brief Generator

Reads tool fixtures + live_data.json and outputs a WordPress-ready HTML
block for the daily SEO update placed below the tool iframe.

Usage:
  python3 daily_brief.py                       # today's matches
  python3 daily_brief.py --date 2026-06-11     # specific date
  python3 daily_brief.py --copy                # also copy output to clipboard (macOS)
  python3 daily_brief.py --out brief.html      # write to file

The brief includes per-match: kickoff, venue, group, Elo-derived win probs,
projected scoreline, estimated line/total, and editorial placeholders that
say [Editorial: ...] so you know where to inject Action Network angles.
"""
import argparse, json, re, sys, math
from datetime import date, datetime
from pathlib import Path

# ---- Config ----
SCRIPT_DIR = Path(__file__).resolve().parent
TOOL_HTML  = SCRIPT_DIR / 'index.html'
LIVE_JSON  = SCRIPT_DIR / 'live_data.json'

# Elo-to-xG mapping. 333 Elo points ~ 1 expected-goal differential — calibrated
# from historical international scoring averages. Base 1.35 reflects WC scoring
# norms (avg ~2.7 goals/game).
ELO_PER_GOAL = 333.0
BASE_XG      = 1.35


def load_tool_data():
    """Extract DATA from the tool's inlined JSON."""
    html = TOOL_HTML.read_text(encoding='utf-8')
    m = re.search(r'DATA = (\{.*?\});\s*\n\s*initApp\(\)', html, re.DOTALL)
    if not m:
        sys.exit(f"ERROR: couldn't find DATA block in {TOOL_HTML}")
    return json.loads(m.group(1))


def load_live_data():
    return json.loads(LIVE_JSON.read_text(encoding='utf-8'))


def elo_to_xg(elo_a, elo_b):
    diff = elo_a - elo_b
    adj = diff / ELO_PER_GOAL
    return max(0.1, BASE_XG + adj), max(0.1, BASE_XG - adj)


def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def match_outcomes(xg_a, xg_b, max_goals=8):
    """Win/draw/loss probabilities via independent Poisson distributions."""
    pw = pd = pl = 0.0
    for ga in range(max_goals):
        for gb in range(max_goals):
            p = poisson_pmf(ga, xg_a) * poisson_pmf(gb, xg_b)
            if ga > gb:   pw += p
            elif ga == gb: pd += p
            else:         pl += p
    s = pw + pd + pl
    return pw/s, pd/s, pl/s


def fmt_pct(x): return f"{round(x*100)}%"


def get_matches_for_date(data, target_date, live=None):
    """Return unique matches on a date, sorted by kickoff.
    For each match, put the higher-Elo team first (matches how books frame the line)."""
    seen, matches = set(), []
    for team, fixtures in data['fixtures'].items():
        for fx in fixtures:
            if fx.get('date') != target_date:
                continue
            key = tuple(sorted([team, fx['opp']]))
            if key in seen:
                continue
            seen.add(key)
            # Order team_a/team_b by Elo (higher first)
            opp = fx['opp']
            if live:
                elo_t   = live['team_meta'].get(team, {}).get('elo', 1500)
                elo_opp = live['team_meta'].get(opp,  {}).get('elo', 1500)
                if elo_opp > elo_t:
                    team, opp = opp, team
            matches.append({
                'team_a': team, 'team_b': opp,
                'kickoff_et': fx['time_et'],
                'stadium': fx['stadium'], 'city': fx['city'],
                'group': fx['group'],
                'date_display': fx['date_display'],
            })

    def time_key(m):
        t = m['kickoff_et'].replace(' ET', '').strip()
        h, rest = t.split(':')
        mn, ampm = rest.split(' ')
        h, mn = int(h), int(mn)
        if ampm == 'PM' and h != 12: h += 12
        if ampm == 'AM' and h == 12: h = 0
        return h * 60 + mn
    matches.sort(key=time_key)
    return matches


def format_brief(matches, target_date, live):
    if not matches:
        return f"<!-- No World Cup matches on {target_date} -->"

    date_obj = datetime.strptime(target_date, '%Y-%m-%d').date()
    nice_date = date_obj.strftime('%A, %B %-d')  # macOS: %-d strips leading 0
    is_opener = (target_date == '2026-06-11')

    out = []
    out.append(f'<h2>World Cup 2026: Today\'s Matches and Action Network Predictions &mdash; {nice_date}</h2>')
    if is_opener:
        out.append(
            '<p>The 2026 FIFA World Cup gets underway today, with the tournament\'s opening '
            'match at Estadio Azteca and a Group A doubleheader to kick off six weeks of soccer '
            'across the United States, Canada, and Mexico. Use the Match Up Tool above to compare '
            'any two of the 48 teams. Below, our Elo-and-Poisson model breaks down today\'s slate.</p>'
        )
    else:
        out.append(
            '<p>Below are today\'s World Cup 2026 matches with Action Network\'s win probability '
            'projections, estimated lines, and matchup notes. Use the tool above to explore any '
            'fixture in depth, then check the daily slate here for picks and game flow.</p>'
        )

    for m in matches:
        ta = live['team_meta'].get(m['team_a'], {})
        tb = live['team_meta'].get(m['team_b'], {})
        elo_a = ta.get('elo', 1500)
        elo_b = tb.get('elo', 1500)
        fifa_a = ta.get('fifa', '—')
        fifa_b = tb.get('fifa', '—')

        xg_a, xg_b = elo_to_xg(elo_a, elo_b)
        pw, pd, pl = match_outcomes(xg_a, xg_b)

        diff = xg_a - xg_b
        # Round to nearest 0.25 (typical Asian handicap increment)
        handicap = round(diff * 4) / 4
        if abs(handicap) < 0.13:
            line_str = "Pick 'em"
        else:
            fav = m['team_a'] if handicap > 0 else m['team_b']
            line_str = f"{fav} -{abs(handicap):.2f}"
        # Round total to nearest 0.5
        total = round((xg_a + xg_b) * 2) / 2

        # Section per match
        out.append('')
        out.append(f"<h3>{m['kickoff_et']}: {m['team_a']} vs {m['team_b']}</h3>")
        out.append(f"<p><strong>Venue:</strong> {m['stadium']}, {m['city']} &middot; <strong>{m['group']}</strong></p>")
        out.append('<ul>')
        out.append(f"<li><strong>Win probability:</strong> {m['team_a']} {fmt_pct(pw)} &middot; Draw {fmt_pct(pd)} &middot; {m['team_b']} {fmt_pct(pl)}</li>")
        out.append(f"<li><strong>Projected score:</strong> {m['team_a']} {xg_a:.1f}, {m['team_b']} {xg_b:.1f}</li>")
        out.append(f"<li><strong>Goal total:</strong> {total} &middot; <strong>Goal handicap:</strong> {line_str}</li>")
        out.append(f"<li><strong>FIFA ranks:</strong> {m['team_a']} #{fifa_a}, {m['team_b']} #{fifa_b} &middot; <strong>Elo:</strong> {elo_a} vs {elo_b}</li>")
        out.append('</ul>')
        out.append(f"<p><em>[Editorial: 1-2 sentences here on form, key player, tactical angle, or betting view. Delete this line after writing.]</em></p>")

    out.append('')
    out.append(f'<p><small><em>Updated {datetime.now().strftime("%B %-d, %Y at %-I:%M %p ET")}. '
               'Matchup probabilities derived from Elo ratings using a Poisson scoring model. '
               'Tournament-level projections and exit distributions in the Match Up Tool above '
               'come from BETSIE, Action Network\'s in-house tournament model.</em></small></p>')

    return '\n'.join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=date.today().isoformat(), help='YYYY-MM-DD (default: today)')
    p.add_argument('--copy', action='store_true', help='Copy output to clipboard (macOS pbcopy)')
    p.add_argument('--out', help='Write to file path')
    args = p.parse_args()

    data = load_tool_data()
    live = load_live_data()
    matches = get_matches_for_date(data, args.date, live)
    brief = format_brief(matches, args.date, live)

    print(brief)
    if args.out:
        Path(args.out).write_text(brief, encoding='utf-8')
        print(f"\n[Wrote {args.out}]", file=sys.stderr)
    if args.copy:
        import subprocess
        try:
            subprocess.run(['pbcopy'], input=brief.encode('utf-8'), check=True)
            print('\n[Copied to clipboard]', file=sys.stderr)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(f'\n[Copy failed: {e}]', file=sys.stderr)


if __name__ == '__main__':
    main()
