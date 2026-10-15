#!/usr/bin/env python3
"""
Novig API client — signed requests, market data, order placement.
Uses Ed25519 signing with NOVIG-V3 scheme.
"""

import os, json, time, hashlib, base64, urllib.parse
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import requests

NOVIG_HOST = 'https://api.novig.com'

# ── Key loading ──────────────────────────────────────────────────────────
def load_key(pem_str):
    """Load Ed25519 private key from PEM string."""
    return load_pem_private_key(pem_str.encode(), password=None)


# ── Signing ──────────────────────────────────────────────────────────────
def _canonicalize_query(query_string):
    """Canonicalize query string per Novig spec."""
    if not query_string:
        return ''
    pairs = query_string.split('&')
    decoded = []
    for pair in pairs:
        if '=' in pair:
            name, value = pair.split('=', 1)
        else:
            name, value = pair, ''
        # URI-decode (+ stays literal)
        name = urllib.parse.unquote(name)
        value = urllib.parse.unquote(value)
        # Re-encode with uppercase %XX, keeping unreserved chars
        name = urllib.parse.quote(name, safe='-._~')
        value = urllib.parse.quote(value, safe='-._~')
        decoded.append((name, value))
    # Sort bytewise
    decoded.sort(key=lambda p: (p[0], p[1]))
    return '&'.join(f'{n}={v}' for n, v in decoded)


def _body_hash(body_bytes):
    """SHA-256 hash of body bytes, lowercase hex."""
    return hashlib.sha256(body_bytes).hexdigest()


def sign_request(private_key, method, path, query='', body=b''):
    """Sign a request using NOVIG-V3 Ed25519 scheme.
    Returns (timestamp_ms, signature_b64).
    """
    ts = str(int(time.time() * 1000))
    canon_query = _canonicalize_query(query)
    body_h = _body_hash(body)

    canonical = f'NOVIG-V3\n{ts}\n{method}\n{path}\n{canon_query}\n{body_h}'

    sig_bytes = private_key.sign(canonical.encode())
    sig_b64 = base64.b64encode(sig_bytes).decode()

    return ts, sig_b64


def _signed_headers(key_id, private_key, method, path, query='', body=b''):
    ts, sig = sign_request(private_key, method, path, query, body)
    return {
        'Novig-Key-Id': key_id,
        'Novig-Timestamp': ts,
        'Novig-Signature': sig,
        'Content-Type': 'application/json',
    }


# ── API client ───────────────────────────────────────────────────────────
class NovigClient:
    def __init__(self, key_id=None, private_key_pem=None):
        self.key_id = key_id or os.environ.get('NOVIG_KEY_ID', '')
        pem = private_key_pem or os.environ.get('NOVIG_PRIVATE_KEY', '')
        if pem:
            self.private_key = load_key(pem)
        else:
            self.private_key = None

    def _request(self, method, path, params=None, body=None):
        query = urllib.parse.urlencode(params) if params else ''
        body_bytes = json.dumps(body).encode() if body else b''
        url = f'{NOVIG_HOST}{path}'
        if query:
            url += '?' + query

        headers = _signed_headers(
            self.key_id, self.private_key,
            method, path, query, body_bytes
        )

        if method == 'GET':
            r = requests.get(url, headers=headers, timeout=15)
        elif method == 'POST':
            r = requests.post(url, headers=headers, data=body_bytes, timeout=15)
        elif method == 'DELETE':
            r = requests.delete(url, headers=headers, data=body_bytes, timeout=15)
        else:
            raise ValueError(f'Unknown method: {method}')

        return r

    def echo(self, message='hello'):
        """Test authentication with /v3/echo."""
        r = self._request('POST', '/v3/echo', body={'message': message})
        return r.status_code, r.json() if r.ok else r.text

    def get_balance(self):
        """Get account balance."""
        r = self._request('GET', '/v3/account/balance')
        return r.json() if r.ok else None

    def list_subaccounts(self):
        """List all subaccounts."""
        r = self._request('GET', '/v3/account/subaccounts')
        return r.json() if r.ok else None

    def create_subaccount(self, label='nba-model'):
        """Create a new subaccount."""
        r = self._request('POST', '/v3/account/subaccounts', body={'label': label})
        return r.status_code, r.json() if r.ok else r.text

    def get_nba_markets(self, market_type=None):
        """Get NBA markets, optionally filtered by type (SPREAD, TOTAL, MONEY).
        Returns list of market dicts.
        """
        params = {'league': 'NBA', 'limit': '1000'}
        if market_type:
            params['marketType'] = market_type
        r = self._request('GET', '/v3/catalog/markets', params=params)
        if not r.ok:
            print(f'Novig markets failed: {r.status_code} {r.text[:200]}')
            return []
        data = r.json()
        return data.get('items', data) if isinstance(data, dict) else data

    def get_book(self, market_id):
        """Get order book for a specific market."""
        r = self._request('GET', f'/v3/catalog/markets/{market_id}/book')
        if not r.ok:
            return None
        return r.json()

    def place_order(self, outcome_id, price, qty):
        """Place a single order.
        price: decimal probability string (e.g. "0.550")
        qty: number of contracts
        """
        body = {
            'outcomeId': outcome_id,
            'price': str(price),
            'qty': qty,
        }
        r = self._request('POST', '/v3/orders', body=body)
        return r.status_code, r.json() if r.ok else r.text

    def cancel_order(self, order_id):
        """Cancel a specific order."""
        r = self._request('DELETE', f'/v3/orders/{order_id}')
        return r.status_code

    def cancel_all_orders(self):
        """Cancel all resting orders."""
        r = self._request('DELETE', '/v3/orders')
        return r.status_code

    def get_orders(self):
        """Get all resting orders."""
        r = self._request('GET', '/v3/account/orders')
        return r.json() if r.ok else []

    def get_positions(self):
        """Get current positions."""
        r = self._request('GET', '/v3/account/positions')
        return r.json() if r.ok else []


# ── Helpers for NBA model integration ────────────────────────────────────
def prob_to_american(p):
    """Convert decimal probability to American odds."""
    if p <= 0 or p >= 1: return 0
    if p >= 0.5: return -round((p / (1 - p)) * 100)
    return round(((1 - p) / p) * 100)


def american_to_prob(odds):
    """Convert American odds to decimal probability."""
    if odds >= 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def extract_novig_lines(markets):
    """Extract spread and total lines from Novig market data.
    Returns dict keyed by event: {event_id: {spread: X, total: Y, outcomes: {...}}}
    """
    events = {}
    for mkt in markets:
        eid = mkt.get('eventId', '')
        mtype = mkt.get('marketType', '')
        mid = mkt.get('marketId', '')

        if eid not in events:
            events[eid] = {'markets': {}, 'event': mkt.get('event', {})}

        outcomes = mkt.get('outcomes', [])
        events[eid]['markets'][mtype] = {
            'marketId': mid,
            'outcomes': outcomes,
        }

    return events


# ── Parse Novig lines for NBA model ──────────────────────────────────────
def get_novig_nba_lines(client, target_date=None):
    """Fetch Novig NBA spreads and totals, pick the primary line per game.
    Novig has multiple alt lines per game — we pick the one with the smallest
    absolute strike for spreads (closest to pick'em) and the median total.

    target_date: 'YYYY-MM-DD' to filter by game date (optional)

    Returns dict: { 'BOS@DET': { spread: {...}, total: {...}, event_id: '...' } }
    """
    from datetime import datetime as dt

    spreads = client.get_nba_markets(market_type='SPREAD')
    totals = client.get_nba_markets(market_type='TOTAL')
    moneys = client.get_nba_markets(market_type='MONEY')

    if not spreads and not totals:
        return {}

    # Group by eventId, optionally filtering by date
    events = {}
    for m in spreads:
        eid = m['eventId']
        if target_date:
            ts = m.get('startsTs', 0)
            if ts:
                game_date = dt.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
                if game_date != target_date:
                    continue
        if eid not in events:
            events[eid] = {'spreads': [], 'totals': []}
        events[eid]['spreads'].append(m)

    for m in totals:
        eid = m['eventId']
        if target_date:
            ts = m.get('startsTs', 0)
            if ts:
                game_date = dt.fromtimestamp(ts / 1000).strftime('%Y-%m-%d')
                if game_date != target_date:
                    continue
        if eid not in events:
            events[eid] = {'spreads': [], 'totals': []}
        events[eid]['totals'].append(m)

    lines = {}
    for eid, ev in events.items():
        if not ev['spreads']:
            continue

        # Pick primary spread: midpoint of all available strikes
        # Novig posts alt lines around the primary — the midpoint of the range
        # matches the actual market line. On ties, pick closer to zero.
        # Pick the spread with the most liquidity (total qty on the book)
        best_liq = -1
        sp = ev['spreads'][0]
        sp_liq = 0
        for m in ev['spreads']:
            try:
                book = client.get_book(m['marketId'])
                total_qty = sum(
                    o.get('qty', 0) if isinstance(o, dict) else 0
                    for orders in (book.get('orders', {}).values() if book else [])
                    for o in (orders if isinstance(orders, list) else [])
                )
                if total_qty > best_liq:
                    best_liq = total_qty
                    sp = m
                    sp_liq = total_qty
            except:
                pass
        if best_liq <= 0:
            # Fallback: midpoint of strikes
            strikes = [float(m['strike']) for m in ev['spreads']]
            primary = round((min(strikes) + max(strikes)) / 2 * 2) / 2
            ev['spreads'].sort(key=lambda m: abs(float(m['strike']) - primary))
            sp = ev['spreads'][0]
        outcomes = sp['outcomes']

        # Extract team names from outcomes (e.g. "NYK -5.5", "PHI +5.5")
        teams = []
        for o in outcomes:
            parts = o['name'].split()
            if parts:
                teams.append(parts[0])
        if len(teams) != 2:
            continue

        strike = float(sp['strike'])
        # The description team (first word in "NYK -5.5") is the home team
        desc_team = sp['description'].split()[0]
        if desc_team == teams[0]:
            home_team, away_team = teams[0], teams[1]
        else:
            home_team, away_team = teams[1], teams[0]

        # Pick primary total: most liquidity, fallback to median strike
        total_data = None
        tot_liq = 0
        if ev['totals']:
            best_tot_liq = -1
            for m in ev['totals']:
                try:
                    book = client.get_book(m['marketId'])
                    tq = sum(
                        o.get('qty', 0) if isinstance(o, dict) else 0
                        for orders in (book.get('orders', {}).values() if book else [])
                        for o in (orders if isinstance(orders, list) else [])
                    )
                    if tq > best_tot_liq:
                        best_tot_liq = tq
                        total_data = m
                        tot_liq = tq
                except:
                    pass
            if best_tot_liq <= 0:
                ev['totals'].sort(key=lambda m: float(m['strike']))
                total_data = ev['totals'][len(ev['totals']) // 2]

        # Get best bid + fillable qty for each spread outcome
        sp_book = None
        try:
            sp_book = client.get_book(sp['marketId'])
        except:
            pass

        def _best_bid(book, outcome_id):
            """Get best bid price and fillable qty at that price."""
            if not book or not book.get('orders'):
                return None, 0
            orders = book['orders'].get(outcome_id, [])
            if not orders:
                return None, 0
            best_price = max(float(o['price']) for o in orders)
            qty_at_best = sum(o['qty'] for o in orders if float(o['price']) == best_price)
            return best_price, qty_at_best

        key = f'{away_team}@{home_team}'
        entry = {
            'event_id': eid,
            'spread': {
                'strike': strike,
                'market_id': sp['marketId'],
                'home_team': home_team,
                'away_team': away_team,
            },
        }
        # In the exchange, to BUY outcome A you cross with bids on outcome B
        # Ask price for A = 1 - best_bid on B
        # Fillable qty for A = qty at best bid on B
        home_oid = None
        away_oid = None
        for o in outcomes:
            if o['name'].startswith(home_team):
                home_oid = o['outcomeId']
                entry['spread']['home_outcome_id'] = o['outcomeId']
            else:
                away_oid = o['outcomeId']
                entry['spread']['away_outcome_id'] = o['outcomeId']

        # To buy HOME spread: cross with bids on AWAY side
        away_bid, away_bid_qty = _best_bid(sp_book, away_oid)
        home_ask = round(1 - away_bid, 3) if away_bid else None
        home_fillable = round(home_ask * away_bid_qty / 100) if home_ask and away_bid_qty else 0
        entry['spread']['home_price'] = home_ask
        entry['spread']['home_fillable'] = home_fillable

        # To buy AWAY spread: cross with bids on HOME side
        home_bid, home_bid_qty = _best_bid(sp_book, home_oid)
        away_ask = round(1 - home_bid, 3) if home_bid else None
        away_fillable = round(away_ask * home_bid_qty / 100) if away_ask and home_bid_qty else 0
        entry['spread']['away_price'] = away_ask
        entry['spread']['away_fillable'] = away_fillable

        if total_data:
            tot_book = None
            try:
                tot_book = client.get_book(total_data['marketId'])
            except:
                pass
            entry['total'] = {
                'strike': float(total_data['strike']),
                'market_id': total_data['marketId'],
            }
            over_oid = None
            under_oid = None
            for o in total_data['outcomes']:
                if 'Over' in o['name']:
                    over_oid = o['outcomeId']
                    entry['total']['over_outcome_id'] = o['outcomeId']
                elif 'Under' in o['name']:
                    under_oid = o['outcomeId']
                    entry['total']['under_outcome_id'] = o['outcomeId']

            # To buy Over: cross with Under bids
            under_bid, under_bid_qty = _best_bid(tot_book, under_oid)
            over_ask = round(1 - under_bid, 3) if under_bid else None
            over_fillable = round(over_ask * under_bid_qty / 100) if over_ask and under_bid_qty else 0
            entry['total']['over_price'] = over_ask
            entry['total']['over_fillable'] = over_fillable

            # To buy Under: cross with Over bids
            over_bid, over_bid_qty = _best_bid(tot_book, over_oid)
            under_ask = round(1 - over_bid, 3) if over_bid else None
            under_fillable = round(under_ask * over_bid_qty / 100) if under_ask and over_bid_qty else 0
            entry['total']['under_price'] = under_ask
            entry['total']['under_fillable'] = under_fillable

        lines[key] = entry

    return lines


# ── CLI test ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    client = NovigClient()
    if not client.private_key:
        print('Set NOVIG_KEY_ID and NOVIG_PRIVATE_KEY env vars')
        exit(1)

    print('Testing echo...')
    status, resp = client.echo()
    print(f'  Status: {status}')
    print(f'  Response: {resp}')

    if status == 200:
        print('\nAuth works! Fetching NBA lines...')
        lines = get_novig_nba_lines(client)
        print(f'{len(lines)} games with lines:')
        for key, data in lines.items():
            sp = data['spread']
            tot = data.get('total', {})
            print(f'  {key}: spread {sp["strike"]:+.1f} | total {tot.get("strike", "—")}')

        print('\nBalance:')
        bal = client.get_balance()
        print(f'  {bal}')
