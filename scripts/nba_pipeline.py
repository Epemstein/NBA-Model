#!/usr/bin/env python3
"""
NBA Auto-Pipeline — runs every 20 min via GitHub Actions
Port of the NBA Algo spreadsheet model:
  DARKO ratings (O-DPM / D-DPM) weighted by minutes →
  team offense & defense ratings → pace-adjusted score projection →
  compare to sportsbook spreads & totals → Telegram alerts on edges.
"""

import os, json, math, time, requests, datetime, re, io, csv
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
RENDER_URL     = os.environ.get('RENDER_URL', '')  # e.g. https://nba-model-XXXX.onrender.com
ODDS_API_KEY   = os.environ.get('ODDS_API_KEY', '16381a969b24dad9592530007f8b51f9')
TG_TOKEN       = os.environ.get('TG_TOKEN', '')
TG_CHAT        = os.environ.get('TG_CHAT', '')
ET             = ZoneInfo('America/New_York')
EDGE_THRESHOLD = 2.0        # points (spread/total), not percentage
LEAGUE_AVG_ORTG = 113.1     # league average points per 100 possessions (update each season)
PACE_MULTIPLIER = 0.95      # from the spreadsheet
HOME_COURT_ADV  = 0.75      # +0.75 home, -0.75 away (1.5 total)
KELLY_FRACTION  = 0.5
KELLY_MAX_PCT   = 3.0

# ── DARKO Google Sheet ───────────────────────────────────────────────────
DARKO_SHEET_ID = '1mhwOLqPu2F9026EQiVxFPIN1t9RGafGpl-dokaIsm9c'
DARKO_CSV_URL  = f'https://docs.google.com/spreadsheets/d/{DARKO_SHEET_ID}/gviz/tq?tqx=out:csv&sheet=DPM'

# ── NBA API headers (required to avoid 403) ──────────────────────────────
NBA_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.nba.com/',
    'Origin': 'https://www.nba.com',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── NBA Team mappings ────────────────────────────────────────────────────
# NBA API team IDs → abbreviations
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

# Odds API full names → abbreviations
NAME_TO_ABR = {
    'Atlanta Hawks':'ATL', 'Boston Celtics':'BOS', 'Brooklyn Nets':'BKN',
    'Charlotte Hornets':'CHA', 'Chicago Bulls':'CHI', 'Cleveland Cavaliers':'CLE',
    'Dallas Mavericks':'DAL', 'Denver Nuggets':'DEN', 'Detroit Pistons':'DET',
    'Golden State Warriors':'GSW', 'Houston Rockets':'HOU', 'Indiana Pacers':'IND',
    'Los Angeles Clippers':'LAC', 'Los Angeles Lakers':'LAL', 'Memphis Grizzlies':'MEM',
    'Miami Heat':'MIA', 'Milwaukee Bucks':'MIL', 'Minnesota Timberwolves':'MIN',
    'New Orleans Pelicans':'NOP', 'New York Knicks':'NYK', 'Oklahoma City Thunder':'OKC',
    'Orlando Magic':'ORL', 'Philadelphia 76ers':'PHI', 'Phoenix Suns':'PHX',
    'Portland Trail Blazers':'POR', 'Sacramento Kings':'SAC', 'San Antonio Spurs':'SAS',
    'Toronto Raptors':'TOR', 'Utah Jazz':'UTA', 'Washington Wizards':'WAS',
}

ABR_TO_NAME = {v: k for k, v in NAME_TO_ABR.items()}

ALL_TEAMS = sorted(NAME_TO_ABR.values())

# ── Helpers ──────────────────────────────────────────────────────────────
def today_et():
    return datetime.datetime.now(ET).strftime('%Y-%m-%d')

def get_et_date(utc_str):
    d = datetime.datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
    return d.astimezone(ET).strftime('%Y-%m-%d')

def fmt_time_et(utc_str):
    try:
        d = datetime.datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        return d.astimezone(ET).strftime('%-I:%M %p ET')
    except:
        return ''

def fmt_spread(n):
    """Format spread: negative = favored, positive = underdog"""
    if n > 0: return f'+{n:.1f}'
    return f'{n:.1f}'

def spread_to_american(spread):
    """Convert a point spread edge to approximate american odds for Kelly calc.
    A 2-point edge on a spread ≈ 52.5% implied → -110 range."""
    # Rough: each point of spread edge ≈ 2.8% win probability
    p = 0.50 + (abs(spread) * 0.028)
    p = min(p, 0.95)
    if p >= 0.5:
        return -round((p / (1 - p)) * 100)
    return round(((1 - p) / p) * 100)

def calc_kelly(edge_points):
    """Simplified Kelly for spread/total betting at -110 odds.
    edge_points = how many points the model differs from the line."""
    # At -110 odds: decimal = 1.909, b = 0.909
    # Each point of edge ≈ 2.8% extra win probability
    b = 0.909
    p = 0.50 + (abs(edge_points) * 0.028)
    p = min(p, 0.95)
    q = 1 - p
    kelly = (b * p - q) / b
    return max(0, kelly) * 100


# ── Fetch DARKO ratings ──────────────────────────────────────────────────
def fetch_darko():
    """Fetch DARKO DPM ratings from the public Google Sheet.
    Note: The DARKO sheet has NO team column — team assignment comes from
    the NBA API minutes data. We key by player name and match later.
    Returns dict: {player_name_lower: {name, dpm, o_dpm, d_dpm}}
    """
    # Try Firestore cache first
    today = today_et()
    if db:
        try:
            snap = db.collection('nba_darko').document(today).get()
            if snap.exists:
                data = snap.to_dict()
                if data.get('players'):
                    print(f'DARKO loaded from Firestore cache ({len(data["players"])} players)')
                    return {p['name'].lower(): p for p in data['players']}
        except Exception as e:
            print(f'DARKO cache miss: {e}')

    print('Fetching DARKO ratings from Google Sheet...')
    r = requests.get(DARKO_CSV_URL, timeout=30)
    r.raise_for_status()

    reader = csv.DictReader(io.StringIO(r.text))
    players = {}
    raw_list = []

    for row in reader:
        # Column is "Player Name" in the DARKO sheet
        name = row.get('Player Name', row.get('Player', '')).strip()
        if not name:
            continue

        o_dpm = 0.0
        d_dpm = 0.0
        dpm = 0.0

        for key in ['Offensive DPM', 'O-DPM', 'ODPM']:
            if key in row and row[key]:
                try: o_dpm = float(row[key]); break
                except: pass
        for key in ['Defensive DPM', 'D-DPM', 'DDPM']:
            if key in row and row[key]:
                try: d_dpm = float(row[key]); break
                except: pass
        for key in ['DPM', 'dpm']:
            if key in row and row[key]:
                try: dpm = float(row[key]); break
                except: pass

        entry = {
            'name': name,
            'dpm': dpm,
            'o_dpm': o_dpm,
            'd_dpm': d_dpm,
        }
        players[name.lower()] = entry
        raw_list.append(entry)

    print(f'DARKO: {len(players)} players loaded')

    # Cache to Firestore
    if db and raw_list:
        try:
            db.collection('nba_darko').document(today).set({
                'players': raw_list,
                'ts': firestore.SERVER_TIMESTAMP,
            })
        except Exception as e:
            print(f'DARKO cache write failed: {e}')

    return players


# ── NBA API params builder ───────────────────────────────────────────────
def _nba_player_params(season, last_n=8):
    return {
        'Conference': '', 'DateFrom': '', 'DateTo': '', 'Division': '',
        'GameScope': '', 'GameSegment': '', 'Height': '', 'ISTRound': '',
        'LastNGames': str(last_n), 'LeagueID': '00', 'Location': '',
        'MeasureType': 'Base', 'Month': '0', 'OpponentTeamID': '0',
        'Outcome': '', 'PORound': '0', 'PaceAdjust': 'N', 'PerMode': 'PerGame',
        'Period': '0', 'PlayerExperience': '', 'PlayerPosition': '',
        'PlusMinus': 'N', 'Rank': 'N', 'Season': season,
        'SeasonSegment': '', 'SeasonType': 'Regular Season',
        'ShotClockRange': '', 'StarterBench': '', 'TeamID': '0',
        'TwoWay': '0', 'VsConference': '', 'VsDivision': '', 'Weight': '',
    }

def _nba_team_params(season):
    return {
        'Conference': '', 'DateFrom': '', 'DateTo': '', 'Division': '',
        'GameScope': '', 'GameSegment': '', 'Height': '', 'ISTRound': '',
        'LastNGames': '0', 'LeagueID': '00', 'Location': '',
        'MeasureType': 'Advanced', 'Month': '0', 'OpponentTeamID': '0',
        'Outcome': '', 'PORound': '0', 'PaceAdjust': 'N', 'PerMode': 'PerGame',
        'Period': '0', 'PlayerExperience': '', 'PlayerPosition': '',
        'PlusMinus': 'N', 'Rank': 'N', 'Season': season,
        'SeasonSegment': '', 'SeasonType': 'Regular Season',
        'ShotClockRange': '', 'StarterBench': '', 'TeamID': '0',
        'TwoWay': '0', 'VsConference': '', 'VsDivision': '', 'Weight': '',
    }

def _previous_season():
    """Return previous NBA season string."""
    now = datetime.datetime.now(ET)
    year = now.year
    if now.month < 10:
        return f'{year - 2}-{str(year - 1)[2:]}'
    return f'{year - 1}-{str(year)[2:]}'


def _nba_get(base_url, params=None, timeout=30):
    """Make an NBA API request, routing through Render proxy if configured.
    stats.nba.com blocks cloud IPs (GitHub Actions, etc.) so the proxy is needed
    for the pipeline to work in CI.
    """
    if RENDER_URL:
        # Route through proxy: /nba-api/ → stats.nba.com/stats/
        # /nba-live/ → cdn.nba.com/static/json/liveData/
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


# ── Fetch player minutes (last 8 games avg) from NBA API ────────────────
def fetch_player_minutes():
    """Fetch average minutes per game over last ~8 games for all players.
    Returns dict: {player_name_lower: {name, team_abr, minutes}}
    """
    today = today_et()

    # Try Firestore cache (refresh every 4 hours)
    if db:
        try:
            snap = db.collection('nba_minutes').document(today).get()
            if snap.exists:
                data = snap.to_dict()
                ts = data.get('_ts', 0)
                if time.time() - ts < 14400 and data.get('players'):
                    print(f'Minutes loaded from cache ({len(data["players"])} players)')
                    return {p['name'].lower(): p for p in data['players']}
        except Exception as e:
            print(f'Minutes cache miss: {e}')

    print('Fetching player minutes from NBA API...')

    # Try current season first, fall back to previous if no data
    for season in [_current_season(), _previous_season()]:
        try:
            params = _nba_player_params(season, last_n=8)
            r = _nba_get('https://stats.nba.com/stats/leaguedashplayerstats',
                         params=params, timeout=45)
            data = r.json()
            rows = data['resultSets'][0]['rowSet']
            if rows:
                print(f'Using season {season} ({len(rows)} players)')
                break
        except Exception as e:
            print(f'Season {season} failed: {e}')
            rows = []
    else:
        # Fall back to stale Firestore cache (any age)
        if db:
            try:
                snap = db.collection('nba_minutes').document(today).get()
                if snap.exists:
                    data = snap.to_dict()
                    if data.get('players'):
                        print(f'Minutes loaded from stale Firestore cache ({len(data["players"])} players)')
                        return {p['name'].lower(): p for p in data['players']}
            except: pass
        print('No player data available from NBA API')
        return {}

    hdrs = data['resultSets'][0]['headers']
    name_idx = hdrs.index('PLAYER_NAME')
    team_idx = hdrs.index('TEAM_ABBREVIATION')
    min_idx = hdrs.index('MIN')
    gp_idx = hdrs.index('GP')

    players = {}
    raw_list = []
    for row in rows:
        name = row[name_idx]
        team = row[team_idx]
        minutes = row[min_idx] or 0
        gp = row[gp_idx] or 0

        if gp < 1 or minutes < 1:
            continue

        entry = {'name': name, 'team_abr': team, 'minutes': round(minutes, 1)}
        players[name.lower()] = entry
        raw_list.append(entry)

    print(f'Minutes: {len(players)} players')

    # Cache
    if db and raw_list:
        try:
            db.collection('nba_minutes').document(today).set({
                'players': raw_list,
                '_ts': int(time.time()),
            })
        except Exception as e:
            print(f'Minutes cache write failed: {e}')

    return players


# ── Fetch team pace stats from NBA API ───────────────────────────────────
# NBA API full name → abbreviation (for team stats which return TEAM_NAME not abbreviation)
_TEAM_NAME_TO_ABR = {v: k for k, v in ABR_TO_NAME.items()}

def fetch_team_pace():
    """Fetch team pace (possessions per 48 min) from NBA API.
    Falls back to Firestore cache if NBA API is unreachable (cloud IP blocks).
    Returns dict: {team_abr: pace} and league_avg_pace.
    """
    # Try Firestore cache first (refreshed by successful API calls)
    if db:
        try:
            snap = db.collection('nba_pace').document('current').get()
            if snap.exists:
                data = snap.to_dict()
                ts = data.get('_ts', 0)
                # Use cache if less than 24 hours old
                if time.time() - ts < 86400 and data.get('paces'):
                    print(f'Pace loaded from Firestore cache ({len(data["paces"])} teams)')
                    return data['paces'], data.get('league_avg_pace', 100.0), data.get('league_avg_ortg', LEAGUE_AVG_ORTG)
        except Exception as e:
            print(f'Pace cache miss: {e}')

    for season in [_current_season(), _previous_season()]:
        try:
            params = _nba_team_params(season)
            r = _nba_get('https://stats.nba.com/stats/leaguedashteamstats',
                         params=params, timeout=45)
            data = r.json()
            rows = data['resultSets'][0]['rowSet']
            if rows:
                print(f'Team stats using season {season}')
                break
        except Exception as e:
            print(f'Team stats season {season} failed: {e}')
            rows = []
    else:
        # Last resort: try stale Firestore cache (any age)
        if db:
            try:
                snap = db.collection('nba_pace').document('current').get()
                if snap.exists:
                    data = snap.to_dict()
                    if data.get('paces'):
                        print(f'Pace loaded from stale Firestore cache')
                        return data['paces'], data.get('league_avg_pace', 100.0), data.get('league_avg_ortg', LEAGUE_AVG_ORTG)
            except: pass
        raise Exception('No team pace data available')

    hdrs = data['resultSets'][0]['headers']

    # The advanced endpoint returns TEAM_NAME (not TEAM_ABBREVIATION)
    team_idx = hdrs.index('TEAM_NAME') if 'TEAM_NAME' in hdrs else hdrs.index('TEAM_ABBREVIATION')
    is_full_name = 'TEAM_NAME' in hdrs and hdrs[team_idx] == 'TEAM_NAME'
    pace_idx = hdrs.index('PACE')
    ortg_idx = hdrs.index('OFF_RATING')

    paces = {}
    ortgs = {}
    for row in rows:
        raw_team = row[team_idx]
        team = _TEAM_NAME_TO_ABR.get(raw_team, raw_team) if is_full_name else raw_team
        paces[team] = row[pace_idx]
        ortgs[team] = row[ortg_idx]

    league_avg_pace = sum(paces.values()) / len(paces) if paces else 100.0
    league_avg_ortg = sum(ortgs.values()) / len(ortgs) if ortgs else LEAGUE_AVG_ORTG

    # Cache to Firestore for CI runs
    if db and paces:
        try:
            db.collection('nba_pace').document('current').set({
                'paces': paces,
                'league_avg_pace': league_avg_pace,
                'league_avg_ortg': league_avg_ortg,
                '_ts': int(time.time()),
            })
        except Exception as e:
            print(f'Pace cache write failed: {e}')

    print(f'Pace: {len(paces)} teams, avg={league_avg_pace:.1f}, avg_ortg={league_avg_ortg:.1f}')
    return paces, league_avg_pace, league_avg_ortg


# ── Fetch today's NBA schedule ───────────────────────────────────────────
def fetch_schedule(target_date=None):
    """Fetch NBA games for a given date from the NBA API scoreboard endpoint.
    Falls back to cached schedule from Firestore if CDN is blocked.
    Returns list of game dicts.
    """
    today = target_date or today_et()

    # Try scoreboardv3 first (has gameLabel for preseason filtering)
    try:
        r = _nba_get('https://stats.nba.com/stats/scoreboardv3',
                     params={'GameDate': today, 'LeagueID': '00'}, timeout=15)
        data = r.json()
        source = 'v3'
    except Exception as e:
        print(f'Scoreboard v3 failed ({e}), trying CDN...')
        try:
            url = 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'
            r = _nba_get(url, timeout=15)
            data = r.json()
            source = 'cdn'
        except Exception as e2:
            print(f'NBA CDN also failed ({e2}), trying Firestore cache...')
            if db:
                snap = db.collection('nba_slate').document(today).get()
                if snap.exists:
                    cached = snap.to_dict()
                    if cached.get('games'):
                        print(f'Schedule loaded from Firestore cache ({len(cached["games"])} games)')
                        return cached['games']
            raise

    games = []
    for g in data.get('scoreboard', {}).get('games', []):
        # Skip preseason games
        game_label = g.get('gameLabel', '')
        game_id = g.get('gameId', '')
        is_preseason = game_label.lower() == 'preseason' or game_id.startswith('001')
        if is_preseason:
            aw_tri = g.get('awayTeam', {}).get('teamTricode', '?')
            hw_tri = g.get('homeTeam', {}).get('teamTricode', '?')
            print(f'  Skipping preseason: {aw_tri} @ {hw_tri}')
            continue

        away_id = g['awayTeam']['teamId']
        home_id = g['homeTeam']['teamId']
        aw = NBA_ID_TO_ABR.get(away_id, g['awayTeam'].get('teamTricode', ''))
        hw = NBA_ID_TO_ABR.get(home_id, g['homeTeam'].get('teamTricode', ''))

        game_time = g.get('gameTimeUTC', '')
        game_status = g.get('gameStatusText', '')

        games.append({
            'id': g.get('gameId', ''),
            'away': aw,
            'home': hw,
            'away_name': ABR_TO_NAME.get(aw, aw),
            'home_name': ABR_TO_NAME.get(hw, hw),
            'game_time_utc': game_time,
            'game_time_et': fmt_time_et(game_time) if game_time else game_status,
            'status': g.get('gameStatus', 1),  # 1=scheduled, 2=in progress, 3=final
            'status_text': game_status,
            # Model outputs (filled later)
            'away_proj': None,
            'home_proj': None,
            'proj_spread': None,
            'proj_total': None,
            'away_off': None, 'away_def': None,
            'home_off': None, 'home_def': None,
            'matchup_pace': None,
            # Odds (filled later)
            'book_spread': None,    # home team spread (negative = favored)
            'book_total': None,
            'spread_edge': None,
            'total_edge': None,
        })

    print(f'Schedule: {len(games)} games today')
    return games


# ── Fetch odds from The Odds API ─────────────────────────────────────────
def fetch_odds(games):
    """Fetch spreads + totals from The Odds API and attach to games."""
    url = (
        'https://api.the-odds-api.com/v4/sports/basketball_nba/odds/'
        f'?apiKey={ODDS_API_KEY}&regions=us&markets=spreads,totals'
        '&oddsFormat=american&bookmakers=betonlineag'
    )

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        odds_data = r.json()
        print(f'Odds: {len(odds_data)} events')
    except Exception as e:
        print(f'Odds fetch failed: {e}')
        return

    today = today_et()
    for ev in odds_data:
        try:
            ev_date = get_et_date(ev['commence_time'])
        except:
            continue
        if ev_date != today:
            continue

        aw_name = ev['away_team']
        hw_name = ev['home_team']
        aw = NAME_TO_ABR.get(aw_name)
        hw = NAME_TO_ABR.get(hw_name)
        if not aw or not hw:
            continue

        gm = next((g for g in games if g['away'] == aw and g['home'] == hw), None)
        if not gm:
            continue

        for bk in ev.get('bookmakers', []):
            if bk['key'] != 'betonlineag':
                continue
            for mkt in bk.get('markets', []):
                if mkt['key'] == 'spreads':
                    # Home team spread
                    ho = next((o for o in mkt['outcomes'] if o['name'] == hw_name), None)
                    if ho:
                        gm['book_spread'] = ho['point']
                elif mkt['key'] == 'totals':
                    ov = next((o for o in mkt['outcomes'] if o['name'] == 'Over'), None)
                    if ov:
                        gm['book_total'] = ov['point']

        if gm['book_spread'] is not None:
            print(f'  {aw}@{hw}: spread={gm["book_spread"]} total={gm["book_total"]}')


# ── Model: compute team ratings ──────────────────────────────────────────
def compute_team_rating(team_abr, darko, minutes):
    """Compute team offense and defense ratings from DARKO + minutes.

    For each player on the team:
      weight = player_minutes / 48
    Scale weights so team total = 240 (5 starters × 48 min)
    Team offense = sum(player_o_dpm × scaled_weight)
    Team defense = sum(player_d_dpm × scaled_weight)
    """
    team_players = []

    # Match DARKO players to team via minutes data (DARKO has no team column)
    for name_lower, mp in minutes.items():
        if mp['team_abr'] != team_abr:
            continue
        # Find matching DARKO entry by name
        dk = darko.get(name_lower)
        if not dk:
            # Try fuzzy: last name match (DARKO has no team field)
            parts = name_lower.split()
            if len(parts) >= 2:
                last = parts[-1]
                dk = next((d for k, d in darko.items() if k.endswith(last)), None)
        if not dk:
            continue

        team_players.append({
            'name': mp['name'],
            'minutes': mp['minutes'],
            'o_dpm': dk['o_dpm'],
            'd_dpm': dk['d_dpm'],
            'dpm': dk['dpm'],
        })

    if not team_players:
        return 0.0, 0.0, []

    # Sort by minutes descending, take players with real minutes
    team_players.sort(key=lambda p: -p['minutes'])

    # Raw weights: minutes / 48
    total_raw = sum(p['minutes'] / 48 for p in team_players)
    if total_raw == 0:
        return 0.0, 0.0, team_players

    # Scale so weights sum to 5.0 (= 240 / 48)
    scale = 5.0 / total_raw

    off_rating = 0.0
    def_rating = 0.0
    for p in team_players:
        w = (p['minutes'] / 48) * scale
        off_rating += p['o_dpm'] * w
        def_rating += p['d_dpm'] * w

    return round(off_rating, 2), round(def_rating, 2), team_players


# ── Model: project game score ────────────────────────────────────────────
def project_game(away_off, away_def, home_off, home_def,
                 away_pace, home_pace, league_avg_pace, league_avg_ortg):
    """Project final score for a game using the spreadsheet model.

    Matchup pace = away_pace + home_pace - league_avg_pace, × 0.95
    Points per 100 = league_avg_ortg + team_offense - opponent_defense
    Projected points = (pts_per_100 / 100) × matchup_pace ± home_court
    """
    # Pace
    matchup_pace = away_pace + home_pace - league_avg_pace
    adj_pace = matchup_pace * PACE_MULTIPLIER

    # Points per 100 possessions
    away_pp100 = league_avg_ortg + away_off + home_def   # home_def: positive = good D for home, bad for away
    # Wait — need to think about sign conventions.
    # In DARKO: positive D-DPM = good defender (reduces opponent scoring)
    # In the spreadsheet: team defense rating is positive = good
    # Score formula: pts_per_100 = league_avg + offense - opponent_defense
    # So if opponent has good defense (positive), it REDUCES your scoring. Correct.

    away_pp100 = league_avg_ortg + away_off - home_def
    home_pp100 = league_avg_ortg + home_off - away_def

    # Scale to projected possessions
    away_pts = (away_pp100 / 100) * adj_pace - HOME_COURT_ADV
    home_pts = (home_pp100 / 100) * adj_pace + HOME_COURT_ADV

    return {
        'away_pts': round(away_pts, 1),
        'home_pts': round(home_pts, 1),
        'matchup_pace': round(adj_pace, 1),
        'spread': round(away_pts - home_pts, 1),  # negative = home favored
        'total': round(away_pts + home_pts, 1),
        'away_pp100': round(away_pp100, 1),
        'home_pp100': round(home_pp100, 1),
    }


# ── Season helper ────────────────────────────────────────────────────────
def _current_season():
    """Return NBA season string like '2026-27' based on current date."""
    now = datetime.datetime.now(ET)
    year = now.year
    # NBA season starts in October; if before October, it's last year's season
    if now.month < 10:
        return f'{year - 1}-{str(year)[2:]}'
    return f'{year}-{str(year + 1)[2:]}'


# ── Dedup ────────────────────────────────────────────────────────────────
def already_notified(date_str, key):
    if not db: return False
    try:
        return db.collection('nba_notified').document(f'{date_str}_{key}').get().exists
    except: return False

def mark_notified(date_str, key):
    if not db: return
    try:
        db.collection('nba_notified').document(f'{date_str}_{key}').set(
            {'ts': firestore.SERVER_TIMESTAMP}
        )
    except: pass


# ── Telegram ─────────────────────────────────────────────────────────────
def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print(f'[TG skip] {text[:80]}...')
        return
    url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    r = requests.post(url, json={
        'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'Markdown'
    }, timeout=10)
    if not r.ok:
        print(f'Telegram error: {r.text}')


# ── Bankroll ─────────────────────────────────────────────────────────────
def get_bankroll():
    if not db:
        return 10000, KELLY_FRACTION, KELLY_MAX_PCT
    try:
        sett = db.collection('nba_bankroll').document('settings').get()
        if sett.exists:
            d = sett.to_dict()
            return (float(d.get('start', 10000)),
                    float(d.get('kelly', KELLY_FRACTION)),
                    float(d.get('maxPct', KELLY_MAX_PCT)))
    except Exception as e:
        print(f'Bankroll fetch failed: {e}')
    return 10000, KELLY_FRACTION, KELLY_MAX_PCT


# ── Minute overrides from Firestore ──────────────────────────────────────
def get_minute_overrides():
    """Fetch manual minute overrides (e.g. injuries → 0 min).
    Returns dict: {player_name_lower: minutes}
    """
    if not db:
        return {}
    try:
        doc = db.collection('nba_overrides').document('minutes').get()
        if doc.exists:
            data = doc.to_dict() or {}
            overrides = data.get('players', {})
            if overrides:
                print(f'Minute overrides: {len(overrides)} entries')
            return {k.lower(): v for k, v in overrides.items()}
    except Exception as e:
        print(f'Minute overrides fetch failed: {e}')
    return {}


# ── Main ─────────────────────────────────────────────────────────────────
def run(override_date=None):
    date = override_date or today_et()
    print(f'NBA Pipeline running for {date}')
    print(f'Season: {_current_season()}')

    # 1. Fetch data
    try:
        darko = fetch_darko()
    except Exception as e:
        print(f'FATAL: DARKO fetch failed: {e}')
        return

    try:
        player_minutes = fetch_player_minutes()
    except Exception as e:
        print(f'FATAL: Minutes fetch failed: {e}')
        return

    try:
        team_paces, league_avg_pace, league_avg_ortg = fetch_team_pace()
    except Exception as e:
        print(f'WARNING: Pace fetch failed ({e}), using defaults')
        team_paces = {}
        league_avg_pace = 100.0
        league_avg_ortg = LEAGUE_AVG_ORTG

    try:
        games = fetch_schedule(date)
    except Exception as e:
        print(f'FATAL: Schedule fetch failed: {e}')
        return

    if not games:
        print('No games today (regular season)')
        # Clear stale data from Firestore
        if db:
            try:
                db.collection('nba_slate').document(date).set({
                    'games': [],
                    'team_ratings': {},
                    'lastUpdated': firestore.SERVER_TIMESTAMP,
                    'lastUpdatedMs': int(time.time() * 1000),
                    'date': date,
                    'message': 'No regular season games today',
                })
                print('Cleared stale Firestore data')
            except: pass
        return

    # Apply minute overrides (injuries, etc.)
    overrides = get_minute_overrides()
    for name_lower, mins in overrides.items():
        if name_lower in player_minutes:
            player_minutes[name_lower]['minutes'] = mins
        # If set to 0, effectively removes the player

    # 2. Fetch odds — try Novig first, fall back to The Odds API
    novig_key_id = os.environ.get('NOVIG_KEY_ID', '')
    novig_pem = os.environ.get('NOVIG_PRIVATE_KEY', '')
    if novig_key_id and novig_pem:
        try:
            try:
                from scripts.novig import NovigClient, get_novig_nba_lines
            except ImportError:
                from novig import NovigClient, get_novig_nba_lines
            nc = NovigClient(novig_key_id, novig_pem)
            novig_lines = get_novig_nba_lines(nc, target_date=date)
            print(f'Novig: {len(novig_lines)} games with lines')
            for gm in games:
                aw, hw = gm['away'], gm['home']
                key = f'{aw}@{hw}'
                nl = novig_lines.get(key)
                if not nl:
                    continue
                sp = nl.get('spread', {})
                tot = nl.get('total', {})
                if sp.get('strike') is not None:
                    gm['book_spread'] = sp['strike']
                    gm['spread_liq'] = sp.get('liquidity', 0)
                    gm['novig_spread_home_oid'] = sp.get('home_outcome_id')
                    gm['novig_spread_away_oid'] = sp.get('away_outcome_id')
                if tot.get('strike') is not None:
                    gm['book_total'] = tot['strike']
                    gm['total_liq'] = tot.get('liquidity', 0)
                    gm['novig_total_over_oid'] = tot.get('over_outcome_id')
                    gm['novig_total_under_oid'] = tot.get('under_outcome_id')
                gm['odds_source'] = 'Novig'
                print(f'  {aw}@{hw}: spread={gm["book_spread"]} total={gm["book_total"]}')
        except Exception as e:
            print(f'Novig fetch failed: {e}')
            fetch_odds(games)
    else:
        fetch_odds(games)

    # 3. Compute team ratings for all teams in today's games
    teams_today = set()
    for g in games:
        teams_today.add(g['away'])
        teams_today.add(g['home'])

    team_ratings = {}
    for team in teams_today:
        off, def_, players = compute_team_rating(team, darko, player_minutes)
        team_ratings[team] = {'off': off, 'def': def_, 'players': players}
        print(f'  {team}: OFF={off:+.2f} DEF={def_:+.2f} ({len(players)} players)')

    # 4. Project each game
    bankroll, kelly_frac, kelly_max = get_bankroll()
    alerts = []

    for gm in games:
        aw, hw = gm['away'], gm['home']
        aw_r = team_ratings.get(aw, {'off': 0, 'def': 0, 'players': []})
        hw_r = team_ratings.get(hw, {'off': 0, 'def': 0, 'players': []})

        aw_pace = team_paces.get(aw, league_avg_pace)
        hw_pace = team_paces.get(hw, league_avg_pace)

        proj = project_game(
            aw_r['off'], aw_r['def'],
            hw_r['off'], hw_r['def'],
            aw_pace, hw_pace,
            league_avg_pace, league_avg_ortg,
        )

        gm['away_proj'] = proj['away_pts']
        gm['home_proj'] = proj['home_pts']
        gm['proj_spread'] = proj['spread']
        gm['proj_total'] = proj['total']
        gm['matchup_pace'] = proj['matchup_pace']
        gm['away_off'] = aw_r['off']
        gm['away_def'] = aw_r['def']
        gm['home_off'] = hw_r['off']
        gm['home_def'] = hw_r['def']
        gm['away_pp100'] = proj['away_pp100']
        gm['home_pp100'] = proj['home_pp100']

        # Attach player rosters (top 10 by minutes)
        gm['away_players'] = [
            {'name': p['name'], 'min': p['minutes'], 'dpm': round(p['dpm'], 1),
             'o_dpm': round(p['o_dpm'], 1), 'd_dpm': round(p['d_dpm'], 1)}
            for p in aw_r.get('players', [])[:10]
        ]
        gm['home_players'] = [
            {'name': p['name'], 'min': p['minutes'], 'dpm': round(p['dpm'], 1),
             'o_dpm': round(p['o_dpm'], 1), 'd_dpm': round(p['d_dpm'], 1)}
            for p in hw_r.get('players', [])[:10]
        ]

        print(f'\n{aw} @ {hw} | {gm["game_time_et"]}')
        print(f'  {aw}: OFF={aw_r["off"]:+.1f} DEF={aw_r["def"]:+.1f} → {proj["away_pts"]} pts')
        print(f'  {hw}: OFF={hw_r["off"]:+.1f} DEF={hw_r["def"]:+.1f} → {proj["home_pts"]} pts')
        print(f'  Pace: {proj["matchup_pace"]} | Spread: {fmt_spread(proj["spread"])} | Total: {proj["total"]}')

        # Compare to odds
        if gm['book_spread'] is not None:
            # book_spread is home team spread (e.g. -5.5 means home favored by 5.5)
            # proj_spread is away_pts - home_pts (negative = home favored)
            # Model home spread = proj_spread (already in the right convention)
            # Edge = how much the model disagrees
            # If model says home -3 and book says home -7, model likes home less → away edge = 4 pts
            model_home_spread = proj['spread']  # negative = home favored
            book_home_spread = gm['book_spread']
            # Spread edge: positive = model favors away more than book
            gm['spread_edge'] = round(model_home_spread - book_home_spread, 1)
            print(f'  Book spread: {fmt_spread(book_home_spread)} | Model: {fmt_spread(model_home_spread)} | Edge: {gm["spread_edge"]:+.1f}')

        if gm['book_total'] is not None:
            gm['total_edge'] = round(proj['total'] - gm['book_total'], 1)
            print(f'  Book total: {gm["book_total"]} | Model: {proj["total"]} | Edge: {gm["total_edge"]:+.1f}')

        # Check for alerts
        date_fmt = datetime.datetime.strptime(date, '%Y-%m-%d').strftime('%m/%d')

        # Spread alert
        if gm['spread_edge'] is not None and abs(gm['spread_edge']) >= EDGE_THRESHOLD:
            edge = gm['spread_edge']
            if edge > 0:
                # Model favors away more than book → bet away (take away spread)
                pick_team = aw
                pick_side = f'{aw} {fmt_spread(-gm["book_spread"])}'
            else:
                # Model favors home more than book → bet home
                pick_team = hw
                pick_side = f'{hw} {fmt_spread(gm["book_spread"])}'

            key = f'{pick_team}_spread'
            if not already_notified(date, key):
                ku = calc_kelly(abs(edge))
                hk_pct = min(ku * kelly_frac, kelly_max)
                risk = round(hk_pct / 100 * bankroll)
                win = round(risk * 0.909)  # -110 payout

                alerts.append({
                    'type': 'spread',
                    'pick': pick_side,
                    'edge': abs(edge),
                    'hk_pct': round(hk_pct, 1),
                    'risk': risk,
                    'win': win,
                    'matchup': f'{aw} @ {hw}',
                    'game_time': gm['game_time_et'],
                    'date_fmt': date_fmt,
                    'date': date,
                    'key': key,
                })

        # Total alert
        if gm['total_edge'] is not None and abs(gm['total_edge']) >= EDGE_THRESHOLD:
            edge = gm['total_edge']
            pick_side = f'{"Over" if edge > 0 else "Under"} {gm["book_total"]}'

            key = f'{aw}{hw}_total'
            if not already_notified(date, key):
                ku = calc_kelly(abs(edge))
                hk_pct = min(ku * kelly_frac, kelly_max)
                risk = round(hk_pct / 100 * bankroll)
                win = round(risk * 0.909)

                alerts.append({
                    'type': 'total',
                    'pick': pick_side,
                    'edge': abs(edge),
                    'hk_pct': round(hk_pct, 1),
                    'risk': risk,
                    'win': win,
                    'matchup': f'{aw} @ {hw}',
                    'game_time': gm['game_time_et'],
                    'date_fmt': date_fmt,
                    'date': date,
                    'key': key,
                })

    # 5. Write to Firestore
    if db:
        try:
            # Strip player lists from game data (too large for Firestore)
            games_out = []
            for g in games:
                out = {k: v for k, v in g.items()}
                games_out.append(out)

            db.collection('nba_slate').document(date).set({
                'games': games_out,
                'team_ratings': {t: {'off': r['off'], 'def': r['def']}
                                 for t, r in team_ratings.items()},
                'league_avg_pace': league_avg_pace,
                'league_avg_ortg': league_avg_ortg,
                'lastUpdated': firestore.SERVER_TIMESTAMP,
                'lastUpdatedMs': int(time.time() * 1000),
                'date': date,
            })
            print(f'\nWrote {len(games_out)} games to Firestore')
        except Exception as e:
            print(f'Firestore write failed: {e}')

    # 6. Send Telegram alerts
    for a in alerts:
        text = (
            f"*NBA Recommended Bet*\n"
            f"{a['pick']} ({a['matchup']}) | {a['game_time']} ({a['date_fmt']})\n"
            f"Edge: {a['edge']:.1f} pts | Half-Kelly: {a['hk_pct']:.1f}%\n"
            f"Bankroll: ${bankroll:,.0f}\n"
            f"Bet: Risk ${a['risk']:,} to win ${a['win']:,}"
        )
        send_telegram(text)
        mark_notified(a['date'], a['key'])
        print(f'Alert: {a["pick"]} edge={a["edge"]:.1f}')

    print(f'\nDone. {len(alerts)} alerts sent.')


if __name__ == '__main__':
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(override_date=date_arg)
