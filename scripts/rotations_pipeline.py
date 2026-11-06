#!/usr/bin/env python3
"""
NBA Rotations Pipeline — fetches play-by-play data for completed games,
extracts substitution events, builds rotation stints (who's on court at
every second), and writes to Firestore.

Usage:
  python rotations_pipeline.py                     # yesterday ET
  python rotations_pipeline.py 2026-10-27           # specific date
  python rotations_pipeline.py --backfill 2026-10-20 2026-10-27  # date range
"""

import os, json, sys, time, datetime, requests
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore

# ── Firebase ─────────────────────────────────────────────────────────────
SA_JSON = os.environ.get('FIREBASE_SA_JSON', '')
db = None
if SA_JSON:
    try:
        sa_data = json.loads(SA_JSON)
        if sa_data.get('type') == 'service_account':
            cred = credentials.Certificate(sa_data)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)
            db = firestore.client()
            print('Firestore connected')
        else:
            print('WARNING: FIREBASE_SA_JSON is not a valid service account — Firestore disabled')
    except Exception as e:
        print(f'WARNING: Failed to init Firebase ({e}) — Firestore disabled')
else:
    print('WARNING: No FIREBASE_SA_JSON — Firestore disabled')

# ── Config ────────────────────────────────────────────────────────────────
RENDER_URL = os.environ.get('RENDER_URL', '')
ET = ZoneInfo('America/New_York')
API_DELAY = 0.6  # seconds between NBA API calls

NBA_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.nba.com/',
    'Origin': 'https://www.nba.com',
    'Accept-Language': 'en-US,en;q=0.9',
}

NBA_ID_TO_ABR = {
    1610612737:'ATL', 1610612738:'BOS', 1610612751:'BKN', 1610612766:'CHA',
    1610612741:'CHI', 1610612739:'CLE', 1610612742:'DAL', 1610612743:'DEN',
    1610612765:'DET', 1610612744:'GSW', 1610612745:'HOU', 1610612754:'IND',
    1610612746:'LAC', 1610612747:'LAL', 1610612763:'MEM', 1610612748:'MIA',
    1610612749:'MIL', 1610612750:'MIN', 1610612740:'NOP', 1610612752:'NYK',
    1610612760:'OKC', 1610612753:'ORL', 1610612755:'PHI', 1610612756:'PHX',
    1610612757:'POR', 1610612758:'SAC', 1610612759:'SAS', 1610612761:'TOR',
    1610612762:'UTA', 1610612764:'WAS',
}


# ── NBA API helper ───────────────────────────────────────────────────────
def _nba_get(base_url, params=None, timeout=30):
    """Make an NBA API request, routing through Render proxy if configured."""
    if RENDER_URL:
        if 'stats.nba.com/stats/' in base_url:
            proxy_url = base_url.replace('https://stats.nba.com/stats/', f'{RENDER_URL}/nba-api/')
        elif 'cdn.nba.com/static/json/liveData/' in base_url:
            proxy_url = base_url.replace('https://cdn.nba.com/static/json/liveData/', f'{RENDER_URL}/nba-live/')
        else:
            proxy_url = base_url
        r = requests.get(proxy_url, params=params, timeout=timeout)
    else:
        r = requests.get(base_url, params=params, headers=NBA_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


# ── Helpers ──────────────────────────────────────────────────────────────
def yesterday_et():
    return (datetime.datetime.now(ET) - datetime.timedelta(days=1)).strftime('%Y-%m-%d')


def period_start_sec(period):
    """Return game-elapsed seconds at the start of a period.
    Q1=0, Q2=720, Q3=1440, Q4=2160, OT1=2880, OT2=3180, ...
    """
    if period <= 4:
        return (period - 1) * 720
    else:
        return 2880 + (period - 5) * 300


def period_duration(period):
    """Duration of a period in seconds. Regulation=720, OT=300."""
    return 720 if period <= 4 else 300


def clock_to_elapsed(period, clock_str):
    """Convert period + game clock string (e.g. 'PT05M30.00S') to game-elapsed seconds.
    Game clock counts DOWN from 12:00 (regulation) or 5:00 (OT).
    """
    base = period_start_sec(period)
    dur = period_duration(period)

    if not clock_str:
        # No clock means end of period
        return base + dur

    # Parse ISO 8601 duration: PT05M30.00S or PT30.00S
    clock_str = clock_str.replace('PT', '').replace('S', '')
    parts = clock_str.split('M')
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
    else:
        minutes = 0
        seconds = float(parts[0])

    remaining = minutes * 60 + seconds
    elapsed_in_period = dur - remaining
    return base + elapsed_in_period


# ── Fetch scoreboard for a date ──────────────────────────────────────────
def fetch_scoreboard(date_str):
    """Fetch all games for a date from the NBA scoreboard v3 endpoint.
    Returns list of game info dicts with game_id, teams, scores, status.
    """
    url = 'https://stats.nba.com/stats/scoreboardv3'
    params = {'GameDate': date_str, 'LeagueID': '00'}

    r = _nba_get(url, params=params)
    data = r.json()

    scoreboard = data.get('scoreboard', {})
    raw_games = scoreboard.get('games', [])

    games = []
    for g in raw_games:
        game_id = g.get('gameId', '')
        status = g.get('gameStatus', 0)  # 1=scheduled, 2=in progress, 3=final
        status_text = g.get('gameStatusText', '')

        home_team = g.get('homeTeam', {})
        away_team = g.get('awayTeam', {})

        home_id = home_team.get('teamId', 0)
        away_id = away_team.get('teamId', 0)

        games.append({
            'game_id': game_id,
            'status': status,
            'status_text': status_text,
            'home': NBA_ID_TO_ABR.get(home_id, str(home_id)),
            'away': NBA_ID_TO_ABR.get(away_id, str(away_id)),
            'home_id': home_id,
            'away_id': away_id,
            'home_score': home_team.get('score', 0),
            'away_score': away_team.get('score', 0),
            'periods': g.get('period', 0),
        })

    return games


# ── Fetch starters from box score ────────────────────────────────────────
def fetch_starters(game_id):
    """Fetch starters for a game from the box score traditional v3 endpoint.
    Returns {home_team_id: [player_ids], away_team_id: [player_ids]}
    """
    url = 'https://stats.nba.com/stats/boxscoretraditionalv3'
    params = {'GameID': game_id}

    r = _nba_get(url, params=params)
    data = r.json()

    box = data.get('boxScoreTraditional', {})
    starters = {}

    for side in ['homeTeam', 'awayTeam']:
        team_data = box.get(side, {})
        team_id = team_data.get('teamId', 0)
        players = team_data.get('players', [])

        team_starters = []
        for p in players:
            stats = p.get('statistics', {})
            if stats.get('startPosition', '') != '':
                team_starters.append({
                    'id': p.get('personId', 0),
                    'name': f"{p.get('firstName', '')} {p.get('familyName', '')}".strip(),
                })

        starters[team_id] = team_starters

    return starters


# ── Fetch play-by-play ───────────────────────────────────────────────────
def fetch_pbp(game_id):
    """Fetch play-by-play v3 data for a game.
    Returns list of action dicts.
    """
    url = 'https://stats.nba.com/stats/playbyplayv3'
    params = {'GameID': game_id}

    r = _nba_get(url, params=params)
    data = r.json()

    game_data = data.get('game', {})
    actions = game_data.get('actions', [])
    return actions


# ── Build rotation stints ────────────────────────────────────────────────
def build_rotations(game_info, starters, actions):
    """Build rotation stints from starters + substitution events.

    Returns dict with player stints per team:
    {
      'away': [{ 'name': str, 'nba_id': int, 'stints': [{start_sec, end_sec, period}], 'total_min': float }],
      'home': [same]
    }
    """
    home_id = game_info['home_id']
    away_id = game_info['away_id']
    num_periods = game_info['periods']

    # Initialize on-court sets from starters
    home_starters = starters.get(home_id, [])
    away_starters = starters.get(away_id, [])

    # Track all players: {player_id: {'name': str, 'team': 'home'|'away', 'stints': [...]}}
    players = {}

    def ensure_player(pid, name, team):
        if pid not in players:
            players[pid] = {'name': name, 'team': team, 'stints': []}

    # Set up starters with stint starting at 0
    home_on_court = set()
    away_on_court = set()

    for p in home_starters:
        pid = p['id']
        ensure_player(pid, p['name'], 'home')
        players[pid]['stints'].append({'start_sec': 0, 'end_sec': None, 'period': 1})
        home_on_court.add(pid)

    for p in away_starters:
        pid = p['id']
        ensure_player(pid, p['name'], 'away')
        players[pid]['stints'].append({'start_sec': 0, 'end_sec': None, 'period': 1})
        away_on_court.add(pid)

    current_period = 1

    for action in actions:
        period = action.get('period', 1)
        clock = action.get('clock', '')
        action_type = action.get('actionType', '')
        team_id = action.get('teamId', 0)

        # Detect period transitions — close all open stints and re-open for new period
        if period != current_period:
            # Close open stints at end of previous period
            period_end = period_start_sec(current_period) + period_duration(current_period)
            for pid in list(home_on_court) + list(away_on_court):
                if pid in players:
                    for stint in players[pid]['stints']:
                        if stint['end_sec'] is None:
                            stint['end_sec'] = period_end

            current_period = period
            period_start = period_start_sec(period)

            # Re-open stints for everyone still on court
            for pid in list(home_on_court):
                if pid in players:
                    players[pid]['stints'].append({
                        'start_sec': period_start, 'end_sec': None, 'period': period
                    })
            for pid in list(away_on_court):
                if pid in players:
                    players[pid]['stints'].append({
                        'start_sec': period_start, 'end_sec': None, 'period': period
                    })

        if action_type != 'substitution':
            continue

        sub_type = action.get('subType', '')
        person_id = action.get('personId', 0)
        player_name = action.get('playerNameI', '') or action.get('playerName', '')

        elapsed = clock_to_elapsed(period, clock)

        if team_id == home_id:
            on_court = home_on_court
            team_label = 'home'
        elif team_id == away_id:
            on_court = away_on_court
            team_label = 'away'
        else:
            continue

        ensure_player(person_id, player_name, team_label)

        if sub_type == 'out':
            # Close the open stint
            for stint in players[person_id]['stints']:
                if stint['end_sec'] is None:
                    stint['end_sec'] = elapsed
            on_court.discard(person_id)

        elif sub_type == 'in':
            # Open a new stint
            players[person_id]['stints'].append({
                'start_sec': elapsed, 'end_sec': None, 'period': period
            })
            on_court.add(person_id)

    # Close any remaining open stints at end of game
    game_end = period_start_sec(num_periods) + period_duration(num_periods)
    for pid, pdata in players.items():
        for stint in pdata['stints']:
            if stint['end_sec'] is None:
                stint['end_sec'] = game_end

    # Build output grouped by team
    home_players = []
    away_players = []

    for pid, pdata in players.items():
        total_sec = sum(max(0, s['end_sec'] - s['start_sec']) for s in pdata['stints'])
        total_min = round(total_sec / 60, 1)

        # Round stint boundaries to nearest second
        stints = []
        for s in pdata['stints']:
            stints.append({
                'start_sec': round(s['start_sec']),
                'end_sec': round(s['end_sec']),
                'period': s['period'],
            })

        entry = {
            'name': pdata['name'],
            'nba_id': pid,
            'stints': stints,
            'total_min': total_min,
        }

        if pdata['team'] == 'home':
            home_players.append(entry)
        else:
            away_players.append(entry)

    # Sort by total minutes descending
    home_players.sort(key=lambda p: -p['total_min'])
    away_players.sort(key=lambda p: -p['total_min'])

    return {'home': home_players, 'away': away_players}


# ── Process a single date ────────────────────────────────────────────────
def process_date(date_str):
    """Fetch and process all completed games for a given date."""
    print(f'\n{"="*60}')
    print(f'Processing {date_str}')
    print(f'{"="*60}')

    # 1. Get scoreboard
    print(f'Fetching scoreboard for {date_str}...')
    scoreboard_games = fetch_scoreboard(date_str)
    time.sleep(API_DELAY)

    if not scoreboard_games:
        print(f'No games found for {date_str}')
        return

    print(f'Found {len(scoreboard_games)} games')

    # Filter to final games only (status=3)
    final_games = [g for g in scoreboard_games if g['status'] == 3]
    skipped = len(scoreboard_games) - len(final_games)
    if skipped:
        print(f'Skipping {skipped} non-final games')

    if not final_games:
        print(f'No completed games for {date_str}')
        return

    print(f'Processing {len(final_games)} completed games')

    # 2. Process each game
    game_results = []

    for i, game in enumerate(final_games):
        gid = game['game_id']
        away = game['away']
        home = game['home']
        print(f'\n  [{i+1}/{len(final_games)}] {away} @ {home} (ID: {gid})')
        print(f'    Score: {away} {game["away_score"]} - {home} {game["home_score"]}')

        try:
            # Fetch starters
            print(f'    Fetching box score for starters...')
            starters = fetch_starters(gid)
            time.sleep(API_DELAY)

            home_starter_count = len(starters.get(game['home_id'], []))
            away_starter_count = len(starters.get(game['away_id'], []))
            print(f'    Starters: {away} ({away_starter_count}), {home} ({home_starter_count})')

            # Fetch PBP
            print(f'    Fetching play-by-play...')
            actions = fetch_pbp(gid)
            time.sleep(API_DELAY)

            sub_count = sum(1 for a in actions if a.get('actionType') == 'substitution')
            print(f'    PBP: {len(actions)} actions, {sub_count} substitutions')

            # Build rotations
            rotations = build_rotations(game, starters, actions)

            home_player_count = len(rotations['home'])
            away_player_count = len(rotations['away'])
            print(f'    Rotations: {away} ({away_player_count} players), {home} ({home_player_count} players)')

            game_results.append({
                'game_id': gid,
                'away': away,
                'home': home,
                'away_score': game['away_score'],
                'home_score': game['home_score'],
                'players': {
                    'away': rotations['away'],
                    'home': rotations['home'],
                },
            })

        except Exception as e:
            print(f'    ERROR processing game {gid}: {e}')
            continue

    # 3. Write to Firestore
    if not game_results:
        print(f'\nNo games successfully processed for {date_str}')
        return

    if db:
        try:
            doc = {
                'games': game_results,
                'date': date_str,
                'ts': firestore.SERVER_TIMESTAMP,
            }
            db.collection('nba_rotations').document(date_str).set(doc)
            print(f'\nWrote {len(game_results)} games to Firestore: nba_rotations/{date_str}')
        except Exception as e:
            print(f'\nFirestore write failed: {e}')
    else:
        print(f'\nFirestore disabled — {len(game_results)} games processed (not written)')
        # Print summary for debugging
        for g in game_results:
            away_mins = sum(p['total_min'] for p in g['players']['away'])
            home_mins = sum(p['total_min'] for p in g['players']['home'])
            print(f"  {g['away']} ({away_mins:.0f} min) @ {g['home']} ({home_mins:.0f} min): "
                  f"{g['away_score']}-{g['home_score']}")


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    if len(args) >= 3 and args[0] == '--backfill':
        # Backfill mode: process a date range
        start_date = datetime.date.fromisoformat(args[1])
        end_date = datetime.date.fromisoformat(args[2])
        print(f'Backfill mode: {start_date} to {end_date}')

        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            try:
                process_date(date_str)
            except Exception as e:
                print(f'ERROR processing {date_str}: {e}')
            current += datetime.timedelta(days=1)

        print(f'\nBackfill complete: {start_date} to {end_date}')

    elif len(args) >= 1 and args[0] != '--backfill':
        # Single date mode
        process_date(args[0])

    else:
        # Default: yesterday ET
        date_str = yesterday_et()
        process_date(date_str)

    print('\nDone.')


if __name__ == '__main__':
    main()
