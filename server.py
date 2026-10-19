#!/usr/bin/env python3
"""
NBA Algo Proxy Server
- Local: python server.py (serves on port 8080 + static files)
- Cloud: Deploy to Render.com (proxy only, no static files needed)
"""
import http.server, urllib.request, json, os

PORT = int(os.environ.get('PORT', 8080))
IS_CLOUD = os.environ.get('RENDER', '') != ''

ODDS_API_KEY = os.environ.get('ODDS_API_KEY', '16381a969b24dad9592530007f8b51f9')

PROXY_RULES = {
    '/nba-api/':    'https://stats.nba.com/stats/',
    '/nba-live/':   'https://cdn.nba.com/static/json/liveData/',
    '/odds-api/':   'https://api.the-odds-api.com/v4/',
    '/darko/':      f'https://docs.google.com/spreadsheets/d/1mhwOLqPu2F9026EQiVxFPIN1t9RGafGpl-dokaIsm9c/',
}

NBA_HEADERS = {
    'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept':          'application/json, text/plain, */*',
    'Referer':         'https://www.nba.com/',
    'Origin':          'https://www.nba.com',
    'Accept-Language': 'en-US,en;q=0.9',
}

CDN_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept':     'application/json',
}

ODDS_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept':     'application/json',
}

SHEETS_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept':     'text/csv, */*',
}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'ok')
            return

        # Novig order book for a single game (all alt lines)
        if self.path.startswith('/novig-book'):
            self._novig_book()
            return

        # Refresh odds endpoint — fetches live Novig data and writes to Firestore
        if self.path.startswith('/refresh-odds'):
            self._refresh_odds()
            return

        if self.path == '/rss/nba':
            self._proxy('https://www.rotowire.com/rss/news.php?sport=NBA', {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/rss+xml, */*'})
            return

        if self.path.startswith('/load-date'):
            self._load_date()
            return

        for prefix, upstream_base in PROXY_RULES.items():
            if self.path.startswith(prefix):
                tail = self.path[len(prefix):]
                upstream = upstream_base + tail

                if 'stats.nba.com' in upstream_base:
                    hdrs = NBA_HEADERS
                elif 'cdn.nba.com' in upstream_base:
                    hdrs = CDN_HEADERS
                elif 'the-odds-api' in upstream_base:
                    hdrs = ODDS_HEADERS
                    sep = '&' if '?' in upstream else '?'
                    upstream = upstream + sep + 'apiKey=' + ODDS_API_KEY
                else:
                    hdrs = SHEETS_HEADERS

                self._proxy(upstream, hdrs)
                return

        if not IS_CLOUD:
            http.server.SimpleHTTPRequestHandler(
                self.request, self.client_address, self.server
            ).do_GET()
            return

        self.send_error(404)

    def _proxy(self, url, headers):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=60)
            data = resp.read()
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type',
                             resp.headers.get('Content-Type', 'application/json'))
            self.send_header('Cache-Control', 'max-age=60')
            self.end_headers()
            self.wfile.write(data)
            print(f'  > {url[:100]}')
        except Exception as e:
            print(f'  x {url[:100]} — {e}')
            self.send_response(502)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _load_date(self):
        """Fetch ESPN schedule for a date and write basic game data to Firestore."""
        import datetime, time, requests as req
        from urllib.parse import urlparse, parse_qs
        from zoneinfo import ZoneInfo

        qs = parse_qs(urlparse(self.path).query)
        ET = ZoneInfo('America/New_York')
        target_date = qs.get('date', [datetime.datetime.now(ET).strftime('%Y-%m-%d')])[0]
        espn_date = target_date.replace('-', '')

        sa_json = os.environ.get('FIREBASE_SA_JSON', '')

        try:
            # Fetch ESPN schedule
            r = req.get(f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={espn_date}', timeout=15)
            r.raise_for_status()
            events = r.json().get('events', [])

            ESPN_ABR = {'GS':'GSW','SA':'SAS','NY':'NYK','NO':'NOP','WSH':'WAS','UTAH':'UTA'}
            NBA_ABR_TO_NAME = {
                'ATL':'Atlanta Hawks','BOS':'Boston Celtics','BKN':'Brooklyn Nets','CHA':'Charlotte Hornets',
                'CHI':'Chicago Bulls','CLE':'Cleveland Cavaliers','DAL':'Dallas Mavericks','DEN':'Denver Nuggets',
                'DET':'Detroit Pistons','GSW':'Golden State Warriors','HOU':'Houston Rockets','IND':'Indiana Pacers',
                'LAC':'Los Angeles Clippers','LAL':'Los Angeles Lakers','MEM':'Memphis Grizzlies','MIA':'Miami Heat',
                'MIL':'Milwaukee Bucks','MIN':'Minnesota Timberwolves','NOP':'New Orleans Pelicans','NYK':'New York Knicks',
                'OKC':'Oklahoma City Thunder','ORL':'Orlando Magic','PHI':'Philadelphia 76ers','PHX':'Phoenix Suns',
                'POR':'Portland Trail Blazers','SAC':'Sacramento Kings','SAS':'San Antonio Spurs','TOR':'Toronto Raptors',
                'UTA':'Utah Jazz','WAS':'Washington Wizards',
            }

            games = []
            for ev in events:
                comps = ev.get('competitions', [{}])[0]
                competitors = comps.get('competitors', [])
                away_comp = next((c for c in competitors if c.get('homeAway') == 'away'), {})
                home_comp = next((c for c in competitors if c.get('homeAway') == 'home'), {})
                aw_raw = away_comp.get('team', {}).get('abbreviation', '')
                hw_raw = home_comp.get('team', {}).get('abbreviation', '')
                aw = ESPN_ABR.get(aw_raw, aw_raw)
                hw = ESPN_ABR.get(hw_raw, hw_raw)
                game_time = ev.get('date', '')
                status = ev.get('status', {}).get('type', {}).get('shortDetail', '')

                # Format time
                try:
                    from datetime import datetime as dt
                    d = dt.fromisoformat(game_time.replace('Z', '+00:00'))
                    time_et = d.astimezone(ET).strftime('%-I:%M %p ET')
                except:
                    time_et = status

                games.append({
                    'id': ev.get('id', ''),
                    'away': aw, 'home': hw,
                    'away_name': NBA_ABR_TO_NAME.get(aw, aw),
                    'home_name': NBA_ABR_TO_NAME.get(hw, hw),
                    'game_time_utc': game_time,
                    'game_time_et': time_et,
                    'status': 1, 'status_text': status,
                    'is_preseason': ev.get('season', {}).get('type', 0) == 1,
                    'away_proj': None, 'home_proj': None,
                    'proj_spread': None, 'proj_total': None,
                    'away_off': None, 'away_def': None,
                    'home_off': None, 'home_def': None,
                    'matchup_pace': None,
                    'book_spread': None, 'book_total': None,
                    'spread_edge': None, 'total_edge': None,
                    'away_players': [], 'home_players': [],
                })

            # Write to Firestore
            if sa_json and games:
                try:
                    import firebase_admin
                    from firebase_admin import credentials, firestore as fs
                    if not firebase_admin._apps:
                        cred = credentials.Certificate(json.loads(sa_json))
                        firebase_admin.initialize_app(cred)
                    db = fs.client()
                    db.collection('nba_slate').document(target_date).set({
                        'games': games,
                        'team_ratings': {},
                        'lastUpdated': fs.SERVER_TIMESTAMP,
                        'lastUpdatedMs': int(time.time() * 1000),
                        'date': target_date,
                    })
                except Exception as e:
                    print(f'Firestore write failed: {e}')

            self._json_response(200, {'games': len(games), 'date': target_date})
        except Exception as e:
            print(f'Load date failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _refresh_odds(self):
        """Fetch live Novig odds and write to Firestore for the dashboard."""
        import datetime, time
        from zoneinfo import ZoneInfo
        try:
            from scripts.novig import NovigClient, get_novig_nba_lines
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.novig import NovigClient, get_novig_nba_lines

        novig_key = os.environ.get('NOVIG_KEY_ID', '')
        novig_pem = os.environ.get('NOVIG_PRIVATE_KEY', '')
        sa_json = os.environ.get('FIREBASE_SA_JSON', '')

        if not novig_key or not novig_pem:
            self._json_response(400, {'error': 'NOVIG keys not configured'})
            return

        try:
            # Parse date from query string (?date=2026-10-20) or use today
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ET = ZoneInfo('America/New_York')
            target_date = qs.get('date', [datetime.datetime.now(ET).strftime('%Y-%m-%d')])[0]

            nc = NovigClient(novig_key, novig_pem)
            lines = get_novig_nba_lines(nc, target_date=target_date)

            # Build response with all odds data
            result = {}
            for key, data in lines.items():
                sp = data.get('spread', {})
                tot = data.get('total', {})
                ml = data.get('money', {})
                result[key] = {
                    'book_spread': sp.get('strike'),
                    'home_spread_price': sp.get('home_price'),
                    'home_spread_fill': sp.get('home_fillable', 0),
                    'away_spread_price': sp.get('away_price'),
                    'away_spread_fill': sp.get('away_fillable', 0),
                    'book_total': tot.get('strike') if tot else None,
                    'over_price': tot.get('over_price') if tot else None,
                    'over_fill': tot.get('over_fillable', 0) if tot else 0,
                    'under_price': tot.get('under_price') if tot else None,
                    'under_fill': tot.get('under_fillable', 0) if tot else 0,
                    'home_ml_price': ml.get('home_price') if ml else None,
                    'home_ml_fill': ml.get('home_fillable', 0) if ml else 0,
                    'away_ml_price': ml.get('away_price') if ml else None,
                    'away_ml_fill': ml.get('away_fillable', 0) if ml else 0,
                }

            # Also write to Firestore if configured
            if sa_json:
                try:
                    import firebase_admin
                    from firebase_admin import credentials, firestore as fs
                    if not firebase_admin._apps:
                        cred = credentials.Certificate(json.loads(sa_json))
                        firebase_admin.initialize_app(cred)
                    db = fs.client()
                    # Update odds fields on the existing slate doc
                    snap = db.collection('nba_slate').document(target_date).get()
                    if snap.exists:
                        slate = snap.to_dict()
                        games = slate.get('games', [])
                        for g in games:
                            gkey = f'{g["away"]}@{g["home"]}'
                            if gkey in result:
                                g.update(result[gkey])
                        slate['games'] = games
                        slate['oddsRefreshedMs'] = int(time.time() * 1000)
                        db.collection('nba_slate').document(target_date).set(slate)
                except Exception as e:
                    print(f'Firestore odds update failed: {e}')

            self._json_response(200, {'lines': result, 'date': target_date, 'count': len(result)})
        except Exception as e:
            print(f'Refresh odds failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _novig_book(self):
        """Fetch all alt spread/total/money lines for a single game."""
        import datetime
        from zoneinfo import ZoneInfo
        try:
            from scripts.novig import NovigClient, get_novig_all_lines
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.novig import NovigClient, get_novig_all_lines

        novig_key = os.environ.get('NOVIG_KEY_ID', '')
        novig_pem = os.environ.get('NOVIG_PRIVATE_KEY', '')

        if not novig_key or not novig_pem:
            self._json_response(400, {'error': 'NOVIG keys not configured'})
            return

        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ET = ZoneInfo('America/New_York')
            target_date = qs.get('date', [datetime.datetime.now(ET).strftime('%Y-%m-%d')])[0]
            game_key = qs.get('game', [''])[0]

            if not game_key or '@' not in game_key:
                self._json_response(400, {'error': 'Missing game param (e.g. BOS@DET)'})
                return

            center_spread = float(qs.get('spread', [0])[0]) if qs.get('spread') else None
            center_total = float(qs.get('total', [0])[0]) if qs.get('total') else None

            nc = NovigClient(novig_key, novig_pem)
            result = get_novig_all_lines(nc, target_date, game_key, center_spread=center_spread, center_total=center_total)
            self._json_response(200, result)
        except Exception as e:
            print(f'Novig book failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _json_response(self, code, data):
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def log_message(self, fmt, *args):
        print(f'[{self.address_string()}] {fmt % args}')

if __name__ == '__main__':
    if not IS_CLOUD:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'\n  NBA Algo {"Cloud Proxy" if IS_CLOUD else "Dashboard"}')
    print(f'  Port: {PORT}\n')
    http.server.HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
