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
            resp = urllib.request.urlopen(req, timeout=30)
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
