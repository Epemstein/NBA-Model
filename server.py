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

        # Refresh odds endpoint — fetches live Novig data and writes to Firestore
        if self.path.startswith('/refresh-odds'):
            self._refresh_odds()
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
