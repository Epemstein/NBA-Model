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

    def do_POST(self):
        if self.path == '/novig-mtm':
            self._novig_mtm()
            return
        if self.path == '/place-bet':
            self._place_bet()
            return
        if self.path == '/fund-subaccount':
            self._fund_subaccount()
            return
        self.send_error(404)

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

        if self.path.startswith('/check-roster'):
            self._check_roster()
            return

        if self.path.startswith('/capture-closing'):
            self._capture_closing_endpoint()
            return

        if self.path == '/novig-account':
            self._novig_account()
            return

        if self.path.startswith('/save-preseason-darko'):
            self._save_preseason_darko()
            return

        if self.path.startswith('/game-logs'):
            self._game_logs()
            return

        if self.path.startswith('/trigger-pipeline'):
            self._trigger_pipeline()
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
                    'home_spread_ladder': sp.get('home_ladder', []),
                    'away_spread_ladder': sp.get('away_ladder', []),
                    'home_spread_oid': sp.get('home_outcome_id', ''),
                    'away_spread_oid': sp.get('away_outcome_id', ''),
                    'book_total': tot.get('strike') if tot else None,
                    'over_price': tot.get('over_price') if tot else None,
                    'over_fill': tot.get('over_fillable', 0) if tot else 0,
                    'under_price': tot.get('under_price') if tot else None,
                    'under_fill': tot.get('under_fillable', 0) if tot else 0,
                    'over_ladder': tot.get('over_ladder', []) if tot else [],
                    'under_ladder': tot.get('under_ladder', []) if tot else [],
                    'over_oid': tot.get('over_outcome_id', '') if tot else '',
                    'under_oid': tot.get('under_outcome_id', '') if tot else '',
                    'home_ml_price': ml.get('home_price') if ml else None,
                    'home_ml_fill': ml.get('home_fillable', 0) if ml else 0,
                    'away_ml_price': ml.get('away_price') if ml else None,
                    'away_ml_fill': ml.get('away_fillable', 0) if ml else 0,
                    'home_ml_ladder': ml.get('home_ml_ladder', []) if ml else [],
                    'away_ml_ladder': ml.get('away_ml_ladder', []) if ml else [],
                    'home_ml_oid': ml.get('home_outcome_id', '') if ml else '',
                    'away_ml_oid': ml.get('away_outcome_id', '') if ml else '',
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

            # Also refresh injuries from ESPN
            inj_count = 0
            try:
                inj_count = self._scrape_injuries_to_slate(target_date, db if sa_json else None)
            except Exception as e:
                print(f'Injury refresh failed (non-fatal): {e}')

            # Capture closing lines for games starting within 30 min
            closing_count = 0
            try:
                closing_count = self._capture_closing_lines(target_date, result, db if sa_json else None)
            except Exception as e:
                print(f'Closing line capture failed (non-fatal): {e}')

            self._json_response(200, {'lines': result, 'date': target_date, 'count': len(result), 'injuries': inj_count, 'closingCaptured': closing_count})
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

    def _novig_account(self):
        """Get Novig positions and open orders."""
        try:
            from scripts.novig import NovigClient
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.novig import NovigClient

        novig_key = os.environ.get('NOVIG_KEY_ID', '')
        novig_pem = os.environ.get('NOVIG_PRIVATE_KEY', '')

        if not novig_key or not novig_pem:
            self._json_response(400, {'error': 'NOVIG keys not configured'})
            return

        try:
            nc = NovigClient(novig_key, novig_pem)
            positions = nc.get_positions()
            orders = nc.get_orders()
            # Settled position history
            try:
                hist_r = nc._request('GET', '/v3/history/positions')
                history = hist_r.json() if hist_r.ok else []
            except:
                history = []

            # Get balance via management key
            balance = None
            mgmt_key = os.environ.get('NOVIG_MGMT_KEY_ID', '')
            mgmt_pem = os.environ.get('NOVIG_MGMT_PRIVATE_KEY', '')
            sub_key_id = os.environ.get('NOVIG_SUB_KEY_ID', '78debf9c-a573-482d-9807-c8bfb227fd9f')
            if mgmt_key and mgmt_pem:
                try:
                    mgmt = NovigClient(mgmt_key, mgmt_pem)
                    r = mgmt._request('GET', f'/v3/account/subaccounts/{sub_key_id}/balance')
                    if r.ok:
                        balance = r.json().get('balance')
                except:
                    pass

            self._json_response(200, {'positions': positions, 'orders': orders, 'balance': balance, 'history': history})
        except Exception as e:
            print(f'Novig account failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _save_preseason_darko(self):
        """Copy a DARKO cache doc to nba_darko/preseason as permanent snapshot."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        source_date = qs.get('date', ['2026-10-20'])[0]
        sa_json = os.environ.get('FIREBASE_SA_JSON', '')
        if not sa_json:
            self._json_response(400, {'error': 'Firebase not configured'})
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore as fs
            if not firebase_admin._apps:
                cred = credentials.Certificate(json.loads(sa_json))
                firebase_admin.initialize_app(cred)
            db = fs.client()
            snap = db.collection('nba_darko').document(source_date).get()
            if not snap.exists:
                self._json_response(404, {'error': f'No DARKO cache for {source_date}'})
                return
            data = snap.to_dict()
            data['sourceDate'] = source_date
            data['savedAt'] = fs.SERVER_TIMESTAMP
            db.collection('nba_darko').document('preseason').set(data)
            count = len(data.get('players', []))
            self._json_response(200, {'success': True, 'players': count, 'sourceDate': source_date})
            print(f'Saved preseason DARKO from {source_date}: {count} players')
        except Exception as e:
            print(f'Save preseason DARKO failed: {e}')
            self._json_response(500, {'error': str(e)})

    def _game_logs(self):
        """Fetch recent box scores for a team from ESPN."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        team_slug = qs.get('team', [''])[0].lower()
        limit = int(qs.get('limit', ['5'])[0])
        if not team_slug:
            self._json_response(400, {'error': 'Missing team param'})
            return
        try:
            # ESPN team abbreviation mapping
            espn_map = {'gsw':'gs','nop':'no','nyk':'ny','sas':'sa','was':'wsh','uta':'utah'}
            espn_team = espn_map.get(team_slug, team_slug)

            # Get schedule
            req = urllib.request.Request(
                f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{espn_team}/schedule?season=2027',
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            resp = urllib.request.urlopen(req, timeout=15)
            sched = json.loads(resp.read())

            # Find completed games
            completed = []
            for ev in sched.get('events', []):
                comp = ev.get('competitions', [{}])[0]
                if comp.get('status', {}).get('type', {}).get('completed', False):
                    completed.append(ev)
            completed = completed[-limit:]  # most recent N

            # Fetch box scores
            games = []
            for ev in completed:
                try:
                    req2 = urllib.request.Request(
                        f'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={ev["id"]}',
                        headers={'User-Agent': 'Mozilla/5.0'}
                    )
                    resp2 = urllib.request.urlopen(req2, timeout=15)
                    summary = json.loads(resp2.read())
                    box = summary.get('boxscore', {})
                    game_info = {'date': ev.get('date', ''), 'name': ev.get('name', ''), 'id': ev['id'], 'players': {}}

                    for team_data in box.get('players', []):
                        team_abr = team_data.get('team', {}).get('abbreviation', '')
                        team_players = []
                        for stat_group in team_data.get('statistics', []):
                            labels = stat_group.get('labels', [])
                            min_idx = labels.index('MIN') if 'MIN' in labels else -1
                            for p in stat_group.get('athletes', []):
                                name = p.get('athlete', {}).get('displayName', '?')
                                nba_id = str(p.get('athlete', {}).get('id', ''))
                                stats = p.get('stats', [])
                                mins = stats[min_idx] if min_idx >= 0 and min_idx < len(stats) else '0'
                                try:
                                    mins = int(mins)
                                except:
                                    mins = 0
                                if mins > 0:
                                    team_players.append({'name': name, 'min': mins, 'nba_id': nba_id})
                        team_players.sort(key=lambda x: -x['min'])
                        game_info['players'][team_abr] = team_players
                    games.append(game_info)
                except Exception as e:
                    print(f'Box score fetch failed for {ev["id"]}: {e}')

            self._json_response(200, {'games': games, 'team': team_slug.upper()})
        except Exception as e:
            print(f'Game logs failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _trigger_pipeline(self):
        """Trigger GitHub Actions pipeline via API."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        no_cache = qs.get('noCache', [''])[0] == '1'
        gh_token = os.environ.get('GH_TOKEN', '')
        if not gh_token:
            self._json_response(400, {'error': 'GH_TOKEN not configured'})
            return
        try:
            body = json.dumps({
                'ref': 'main',
                'inputs': {'no_cache': 'true'} if no_cache else {}
            }).encode()
            req = urllib.request.Request(
                'https://api.github.com/repos/Epemstein/NBA-Model/actions/workflows/nba_pipeline.yml/dispatches',
                data=body,
                headers={
                    'Authorization': f'Bearer {gh_token}',
                    'Accept': 'application/vnd.github+json',
                    'Content-Type': 'application/json',
                    'X-GitHub-Api-Version': '2022-11-28',
                },
                method='POST'
            )
            resp = urllib.request.urlopen(req, timeout=15)
            self._json_response(200, {'success': True, 'status': resp.status})
        except Exception as e:
            print(f'Trigger pipeline failed: {e}')
            self._json_response(500, {'error': str(e)})

    def _check_roster(self):
        """Fetch live roster from nba.com team page (__NEXT_DATA__)."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        team_slug = qs.get('team', [''])[0].lower()
        if not team_slug:
            self._json_response(400, {'error': 'Missing team param (e.g. team=bucks)'})
            return
        try:
            url = f'https://www.nba.com/{team_slug}/roster'
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml',
            })
            resp = urllib.request.urlopen(req, timeout=30)
            html = resp.read().decode('utf-8', errors='replace')
            # Extract __NEXT_DATA__ JSON
            marker = '<script id="__NEXT_DATA__" type="application/json">'
            start = html.find(marker)
            if start < 0:
                self._json_response(502, {'error': 'Could not find __NEXT_DATA__'})
                return
            start += len(marker)
            end = html.find('</script>', start)
            next_data = json.loads(html[start:end])
            roster = next_data.get('props', {}).get('pageProps', {}).get('rosterData', {}).get('roster', [])
            players = [{'name': p.get('name', ''), 'nba_id': str(p.get('id', ''))} for p in roster]
            self._json_response(200, {'players': players, 'source': 'nba.com'})
        except Exception as e:
            print(f'Check roster failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(502, {'error': str(e)})

    def _novig_mtm(self):
        """Fetch current bid prices for given positions to calculate mark-to-market."""
        try:
            from scripts.novig import NovigClient
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.novig import NovigClient

        novig_key = os.environ.get('NOVIG_KEY_ID', '')
        novig_pem = os.environ.get('NOVIG_PRIVATE_KEY', '')
        if not novig_key or not novig_pem:
            self._json_response(400, {'error': 'NOVIG keys not configured'})
            return

        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            positions = body.get('positions', [])  # [{outcomeId, marketId, qty, cost}]

            if not positions:
                self._json_response(200, {'mtm': {}})
                return

            nc = NovigClient(novig_key, novig_pem)
            mtm = {}

            # Group by marketId to minimize API calls
            by_market = {}
            for p in positions:
                mid = p.get('marketId', '')
                if mid not in by_market:
                    by_market[mid] = []
                by_market[mid].append(p)

            for mid, pos_list in by_market.items():
                try:
                    book = nc.get_book(mid)
                    if not book or not book.get('orders'):
                        continue
                    for p in pos_list:
                        oid = p['outcomeId']
                        orders = book['orders'].get(oid, [])
                        if orders:
                            best_bid = max(float(o['price']) for o in orders)
                            qty = p.get('qty', 0)
                            cost = float(p.get('cost', 0))
                            value = best_bid * qty / 100  # current value at best bid
                            mtm[oid] = round(value - cost, 2)
                except Exception as e:
                    print(f'MTM book fetch failed for {mid}: {e}')

            self._json_response(200, {'mtm': mtm})
        except Exception as e:
            print(f'MTM failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _fund_subaccount(self):
        """Transfer funds from main wallet to trading subaccount."""
        try:
            from scripts.novig import NovigClient
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.novig import NovigClient

        mgmt_key = os.environ.get('NOVIG_MGMT_KEY_ID', '')
        mgmt_pem = os.environ.get('NOVIG_MGMT_PRIVATE_KEY', '')
        sub_key_id = os.environ.get('NOVIG_SUB_KEY_ID', '78debf9c-a573-482d-9807-c8bfb227fd9f')

        if not mgmt_key or not mgmt_pem:
            self._json_response(400, {'error': 'Management key not configured (NOVIG_MGMT_KEY_ID/NOVIG_MGMT_PRIVATE_KEY)'})
            return

        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            amount = body.get('amount', 0)

            if not amount or amount <= 0:
                self._json_response(400, {'error': 'Invalid amount'})
                return

            nc = NovigClient(mgmt_key, mgmt_pem)

            # Transfer to subaccount
            r = nc._request('POST', f'/v3/account/subaccounts/{sub_key_id}/transfer', body={
                'amount': str(round(amount, 2)),
                'direction': 'fund',
            })

            print(f'Fund subaccount: ${amount} → {r.status_code}')
            try:
                resp = r.json()
            except:
                resp = r.text
            print(f'  Response: {resp}')

            if r.status_code in (200, 201, 202):
                # Get updated balances
                main_bal = nc.get_balance()
                sub_bal_r = nc._request('GET', f'/v3/account/subaccounts/{sub_key_id}/balance')
                sub_bal = sub_bal_r.json() if sub_bal_r.ok else None

                self._json_response(200, {
                    'success': True,
                    'transfer': resp,
                    'mainBalance': main_bal.get('balance') if main_bal else None,
                    'subBalance': sub_bal.get('balance') if sub_bal else None,
                })
            else:
                self._json_response(200, {
                    'success': False,
                    'error': resp.get('message', str(resp)) if isinstance(resp, dict) else str(resp),
                })
        except Exception as e:
            print(f'Fund subaccount failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _place_bet(self):
        """Place a bet on Novig exchange."""
        import time
        try:
            from scripts.novig import NovigClient
        except ImportError:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.novig import NovigClient

        novig_key = os.environ.get('NOVIG_KEY_ID', '')
        novig_pem = os.environ.get('NOVIG_PRIVATE_KEY', '')
        sa_json = os.environ.get('FIREBASE_SA_JSON', '')

        if not novig_key or not novig_pem:
            self._json_response(400, {'error': 'NOVIG keys not configured'})
            return

        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            outcome_id = body.get('outcomeId', '')
            price = body.get('price')
            stake = body.get('stake', 0)
            description = body.get('description', '')
            game_key = body.get('gameKey', '')
            date = body.get('date', '')
            bet_type = body.get('type', '')
            side = body.get('side', '')

            if not outcome_id or not price or not stake:
                self._json_response(400, {'error': 'Missing required fields: outcomeId, price, stake'})
                return

            # Calculate qty: dollars = price * qty / 100 → qty = stake * 100 / price
            tif = body.get('tif', 'IOC')
            qty = body.get('qty') or round(stake * 100 / price)

            # Novig price grid: 0.005 ticks
            price_rounded = round(round(price / 0.005) * 0.005, 3)
            price_str = str(price_rounded)
            print(f'=== PLACE BET ===')
            print(f'  Description: {description}')
            print(f'  Game: {game_key} ({date})')
            print(f'  outcomeId: {outcome_id}')
            print(f'  price: {price_str} (decimal prob)')
            print(f'  qty: {qty} (from stake=${stake})')
            print(f'  tif: {tif}')
            print(f'  side: {side}, type: {bet_type}')

            nc = NovigClient(novig_key, novig_pem)
            status_code, response = nc.place_order(outcome_id, price_str, qty, tif=tif)

            print(f'  Novig HTTP: {status_code}')
            print(f'  Novig body: {response}')
            print(f'=================')

            if status_code in (200, 201):
                # Record in Firestore
                if sa_json:
                    try:
                        import firebase_admin
                        from firebase_admin import credentials, firestore as fs
                        if not firebase_admin._apps:
                            cred = credentials.Certificate(json.loads(sa_json))
                            firebase_admin.initialize_app(cred)
                        db = fs.client()
                        # Check if game starts within 10 min → freeze CLV immediately
                        primary_price = body.get('primaryPrice')
                        closing_price = None
                        try:
                            import datetime as _dt
                            from zoneinfo import ZoneInfo as _ZI
                            _ET = _ZI('America/New_York')
                            _now = _dt.datetime.now(_ET)
                            _slate = db.collection('nba_slate').document(date).get()
                            if _slate.exists:
                                for _g in _slate.to_dict().get('games', []):
                                    if f'{_g["away"]}@{_g["home"]}' == game_key:
                                        _gt = _dt.datetime.fromisoformat(_g.get('game_time_utc','').replace('Z','+00:00'))
                                        if (_gt.astimezone(_ET) - _now).total_seconds() / 60 <= 10:
                                            closing_price = primary_price  # game imminent → CLV = 0
                                        break
                        except:
                            pass

                        bet_record = {
                            'date': date,
                            'gameKey': game_key,
                            'type': bet_type,
                            'description': description,
                            'side': side,
                            'strike': body.get('strike'),
                            'outcomeId': outcome_id,
                            'price': price,
                            'qty': qty,
                            'stake': stake,
                            'ev': body.get('ev'),
                            'primaryPriceAtBet': primary_price,
                            'novigResponse': response if isinstance(response, dict) else {'raw': str(response)},
                            'placedAt': fs.SERVER_TIMESTAMP,
                            'placedAtMs': int(time.time() * 1000),
                        }
                        if closing_price is not None:
                            bet_record['closingPrimaryPrice'] = closing_price
                        db.collection('nba_bets').add(bet_record)
                    except Exception as e:
                        print(f'Firestore bet record failed (bet was placed!): {e}')

                self._json_response(200, {
                    'success': True,
                    'novigStatus': status_code,
                    'response': response if isinstance(response, dict) else str(response),
                })
            else:
                # Extract meaningful error message from Novig response
                err_msg = f'Novig returned {status_code}'
                if isinstance(response, dict):
                    err_msg = response.get('message', response.get('error', err_msg))
                elif isinstance(response, str) and response:
                    err_msg = response[:200]
                self._json_response(200, {
                    'success': False,
                    'error': err_msg,
                    'novigStatus': status_code,
                    'response': response if isinstance(response, dict) else str(response),
                    'request': {'outcomeId': outcome_id, 'price': price_str, 'qty': qty},
                })
        except Exception as e:
            print(f'Place bet failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _capture_closing_endpoint(self):
        """Standalone endpoint to capture closing lines — called by GitHub Actions cron."""
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

        if not sa_json:
            self._json_response(400, {'error': 'Firebase not configured'})
            return

        try:
            ET = ZoneInfo('America/New_York')
            target_date = datetime.datetime.now(ET).strftime('%Y-%m-%d')

            # Fetch current Novig lines
            nc = NovigClient(novig_key, novig_pem) if novig_key and novig_pem else None
            odds_result = {}
            if nc:
                lines = get_novig_nba_lines(nc, target_date=target_date)
                for key, data in lines.items():
                    sp = data.get('spread', {})
                    tot = data.get('total', {})
                    ml = data.get('money', {})
                    odds_result[key] = {
                        'home_spread_price': sp.get('home_price'),
                        'away_spread_price': sp.get('away_price'),
                        'home_ml_price': ml.get('home_price') if ml else None,
                        'away_ml_price': ml.get('away_price') if ml else None,
                        'over_price': tot.get('over_price') if tot else None,
                        'under_price': tot.get('under_price') if tot else None,
                    }

            import firebase_admin
            from firebase_admin import credentials, firestore as fs
            if not firebase_admin._apps:
                cred = credentials.Certificate(json.loads(sa_json))
                firebase_admin.initialize_app(cred)
            db = fs.client()

            count = self._capture_closing_lines(target_date, odds_result, db)
            self._json_response(200, {'captured': count, 'date': target_date, 'games': len(odds_result)})
        except Exception as e:
            print(f'Capture closing failed: {e}')
            import traceback; traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _capture_closing_lines(self, target_date, odds_result, db):
        """Capture closing line prices for bets on games starting within 10 minutes."""
        import datetime, time
        from zoneinfo import ZoneInfo
        if not db:
            return 0

        ET = ZoneInfo('America/New_York')
        now = datetime.datetime.now(ET)

        # Get slate to check game times
        snap = db.collection('nba_slate').document(target_date).get()
        if not snap.exists:
            return 0
        slate = snap.to_dict()
        games = slate.get('games', [])

        # Find games starting within 10 minutes
        closing_games = set()
        for g in games:
            try:
                from datetime import datetime as dt
                gt = dt.fromisoformat(g.get('game_time_utc', '').replace('Z', '+00:00'))
                gt_et = gt.astimezone(ET)
                mins_until = (gt_et - now).total_seconds() / 60
                if -15 <= mins_until <= 10:
                    closing_games.add(f'{g["away"]}@{g["home"]}')
                    print(f'  Game {g["away"]}@{g["home"]} starts in {mins_until:.0f} min — capturing closing lines')
            except:
                pass

        if not closing_games:
            return 0

        # Get all bets for today that don't have closingPrimaryPrice yet
        bets = db.collection('nba_bets').where('date', '==', target_date).get()
        updated = 0
        for bet_doc in bets:
            bet = bet_doc.to_dict()
            if bet.get('closingPrimaryPrice') is not None:
                continue  # already frozen
            if bet.get('gameKey') not in closing_games:
                continue  # game not starting soon

            # Find the current primary price for this bet's side
            gkey = bet['gameKey']
            odds = odds_result.get(gkey, {})
            closing_price = None
            btype = bet.get('type', '')
            side = bet.get('side', '')
            if btype == 'spread':
                closing_price = odds.get('home_spread_price' if side == 'home' else 'away_spread_price')
            elif btype == 'ml':
                closing_price = odds.get('home_ml_price' if side == 'home' else 'away_ml_price')
            elif btype == 'total':
                closing_price = odds.get('over_price' if side == 'over' else 'under_price')

            if closing_price is not None:
                bet_doc.reference.update({'closingPrimaryPrice': closing_price})
                updated += 1
                print(f'  Captured closing line for {bet.get("description")}: {closing_price:.4f}')

        return updated

    def _scrape_injuries_to_slate(self, target_date, db):
        """Scrape ESPN injuries page and update player injury fields on the slate."""
        import re, time
        req = urllib.request.Request(
            'https://www.espn.com/nba/injuries',
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        resp = urllib.request.urlopen(req, timeout=20)
        html = resp.read().decode('utf-8', errors='replace')

        STATUS_MAP = {'Out': 'O', 'Day-To-Day': 'DTD', 'Questionable': 'Q', 'Doubtful': 'D', 'Probable': 'P'}
        pattern = re.compile(
            r'nba/player/_/id/(\d+)/[^\"]*\">([^<]+)</a></td>'
            r'<td[^>]*>([^<]*)</td>'
            r'<td[^>]*>([^<]*)</td>'
            r'<td[^>]*><span[^>]*>([^<]+)</span></td>'
            r'<td[^>]*>([^<]*)</td>',
            re.DOTALL
        )

        # Build lookup: norm_name -> {status, detail}
        # Also build last_name+first_letter index for fuzzy matching (Nic/Nicolas, Mike/Michael)
        injuries = {}
        _inj_by_last_fl = {}
        for m in pattern.finditer(html):
            espn_id, name, position, return_date, status_raw, comment = m.groups()
            status = STATUS_MAP.get(status_raw.strip(), status_raw.strip())
            detail = comment.strip() if comment.strip() else return_date.strip()
            nn = name.strip().lower().replace('.', '')
            for sfx in [' jr', ' sr', ' iv', ' iii', ' ii']:
                if nn.endswith(sfx):
                    nn = nn[:-len(sfx)].strip()
            inj_entry = {'status': status, 'detail': detail, 'name': name.strip()}
            injuries[nn] = inj_entry
            # Index by first_letter + last_name for fuzzy match
            parts = nn.split()
            if parts and len(parts) >= 2:
                fuzzy_key = parts[0][0] + '|' + parts[-1]
                _inj_by_last_fl[fuzzy_key] = inj_entry

        print(f'ESPN injuries scraped: {len(injuries)} players')

        if not db:
            return len(injuries)

        # Update slate games with fresh injury data
        snap = db.collection('nba_slate').document(target_date).get()
        if not snap.exists:
            return len(injuries)

        slate = snap.to_dict()
        games = slate.get('games', [])
        updated = 0
        for g in games:
            for side in ['away_players', 'home_players']:
                for p in (g.get(side) or []):
                    pn = p.get('name', '').lower().replace('.', '')
                    for sfx in [' jr', ' sr', ' iv', ' iii', ' ii']:
                        if pn.endswith(sfx):
                            pn = pn[:-len(sfx)].strip()
                    inj = injuries.get(pn)
                    if not inj and pn.split() and len(pn.split()) >= 2:
                        fuzzy_key = pn.split()[0][0] + '|' + pn.split()[-1]
                        inj = _inj_by_last_fl.get(fuzzy_key)
                    if inj:
                        p['injury'] = inj['status']
                        p['injury_detail'] = inj['detail']
                        updated += 1
                    else:
                        if p.get('injury'):
                            p['injury'] = ''
                            p['injury_detail'] = ''

        slate['games'] = games
        slate['injuriesRefreshedMs'] = int(time.time() * 1000)
        db.collection('nba_slate').document(target_date).set(slate)
        print(f'Injuries updated on slate: {updated} players')

        # Also update nba_rosters/all (used by game card rotation editor)
        roster_updated = 0
        try:
            rsnap = db.collection('nba_rosters').document('all').get()
            if rsnap.exists:
                rdata = rsnap.to_dict()
                teams = rdata.get('teams', {})
                for team_abr, players in teams.items():
                    for p in players:
                        pn = p.get('name', '').lower().replace('.', '')
                        for sfx in [' jr', ' sr', ' iv', ' iii', ' ii']:
                            if pn.endswith(sfx):
                                pn = pn[:-len(sfx)].strip()
                        inj = injuries.get(pn)
                        if not inj and pn.split() and len(pn.split()) >= 2:
                            fuzzy_key = pn.split()[0][0] + '|' + pn.split()[-1]
                            inj = _inj_by_last_fl.get(fuzzy_key)
                        if inj:
                            p['injury'] = inj['status']
                            p['injury_detail'] = inj['detail']
                            roster_updated += 1
                        else:
                            if p.get('injury'):
                                p['injury'] = ''
                                p['injury_detail'] = ''
                rdata['teams'] = teams
                db.collection('nba_rosters').document('all').set(rdata)
                print(f'Injuries updated on rosters: {roster_updated} players')
        except Exception as e:
            print(f'Roster injury update failed (non-fatal): {e}')

        return updated + roster_updated

    def _json_response(self, code, data):
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def log_message(self, fmt, *args):
        print(f'[{self.address_string()}] {fmt % args}')

if __name__ == '__main__':
    if not IS_CLOUD:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print(f'\n  NBA Algo {"Cloud Proxy" if IS_CLOUD else "Dashboard"}')
    print(f'  Port: {PORT}\n')
    http.server.HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
