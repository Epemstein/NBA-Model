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
    from zoneinfo import ZoneInfo
    ET = ZoneInfo('America/New_York')

    spreads = client.get_nba_markets(market_type='SPREAD')
    totals = client.get_nba_markets(market_type='TOTAL')
    moneys = client.get_nba_markets(market_type='MONEY')
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
                game_date = dt.fromtimestamp(ts / 1000, tz=ET).strftime('%Y-%m-%d')
                if game_date != target_date:
                    continue
        if eid not in events:
            events[eid] = {'spreads': [], 'totals': [], 'money': None}
        events[eid]['spreads'].append(m)

    for m in totals:
        eid = m['eventId']
        if target_date:
            ts = m.get('startsTs', 0)
            if ts:
                game_date = dt.fromtimestamp(ts / 1000, tz=ET).strftime('%Y-%m-%d')
                if game_date != target_date:
                    continue
        if eid not in events:
            events[eid] = {'spreads': [], 'totals': [], 'money': None}
        events[eid]['totals'].append(m)

    for m in moneys:
        eid = m['eventId']
        if target_date:
            ts = m.get('startsTs', 0)
            if ts:
                game_date = dt.fromtimestamp(ts / 1000, tz=ET).strftime('%Y-%m-%d')
                if game_date != target_date:
                    continue
        if eid not in events:
            events[eid] = {'spreads': [], 'totals': [], 'money': None}
        events[eid]['money'] = m

    lines = {}
    for eid, ev in events.items():
        if not ev['spreads']:
            continue

        # Extract team names from first spread market
        first_sp = ev['spreads'][0]
        teams = []
        for o in first_sp.get('outcomes', []):
            parts = o['name'].split()
            if parts:
                teams.append(parts[0])
        if len(teams) != 2:
            continue
        desc_team = first_sp['description'].split()[0]
        if desc_team == teams[0]:
            home_team, away_team = teams[0], teams[1]
        else:
            home_team, away_team = teams[1], teams[0]

        # Pick primary spread: most balanced prices (closest to 50/50)
        ev['spreads'].sort(key=lambda m: float(m['strike']))
        sp = ev['spreads'][len(ev['spreads']) // 2]  # fallback to median
        best_balance = 999
        for m in ev['spreads']:
            outs = m.get('outcomes', [])
            if len(outs) != 2:
                continue
            p1 = outs[0].get('price')
            p2 = outs[1].get('price')
            if p1 is None or p2 is None:
                continue
            balance = abs(float(p1) - float(p2))
            if balance < best_balance:
                best_balance = balance
                sp = m
        outcomes = sp['outcomes']
        strike = float(sp['strike'])

        # Pick primary total: most balanced prices (closest to 50/50)
        total_data = None
        if ev['totals']:
            ev['totals'].sort(key=lambda m: float(m['strike']))
            total_data = ev['totals'][len(ev['totals']) // 2]  # fallback
            best_tb = 999
            for m in ev['totals']:
                outs = m.get('outcomes', [])
                if len(outs) != 2:
                    continue
                p1 = outs[0].get('price')
                p2 = outs[1].get('price')
                if p1 is None or p2 is None:
                    continue
                bal = abs(float(p1) - float(p2))
                if bal < best_tb:
                    best_tb = bal
                    total_data = m

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

        # Moneyline
        ml_market = ev.get('money')
        if ml_market:
            ml_book = None
            try:
                ml_book = client.get_book(ml_market['marketId'])
            except:
                pass
            entry['money'] = {'market_id': ml_market['marketId']}
            # desc is just team name like "DET"
            for o in ml_market.get('outcomes', []):
                oname = o['name'].strip()
                bid_price, bid_qty = _best_bid(ml_book, o['outcomeId'])
                # To buy this ML: cross with the OTHER side's bids
                other_oid = [x['outcomeId'] for x in ml_market['outcomes'] if x['outcomeId'] != o['outcomeId']]
                if other_oid:
                    other_bid, other_qty = _best_bid(ml_book, other_oid[0])
                    ask = round(1 - other_bid, 3) if other_bid else None
                    fillable = round(ask * other_qty / 100) if ask and other_qty else 0
                else:
                    ask = None
                    fillable = 0

                if oname == home_team:
                    entry['money']['home_price'] = ask
                    entry['money']['home_fillable'] = fillable
                    entry['money']['home_outcome_id'] = o['outcomeId']
                else:
                    entry['money']['away_price'] = ask
                    entry['money']['away_fillable'] = fillable
                    entry['money']['away_outcome_id'] = o['outcomeId']

        lines[key] = entry

    return lines


# ── All alt lines for a single game ─────────────────────────────────────
def get_novig_all_lines(client, target_date, game_key, center_spread=None, center_total=None):
    """Fetch ALL alt spread/total/money markets for a specific game.
    game_key: 'AWAY@HOME' e.g. 'BOS@DET'
    Returns { spreads: [...], totals: [...], money: {...} }
    """
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    ET = ZoneInfo('America/New_York')

    away_team, home_team = game_key.split('@')

    spreads_raw = client.get_nba_markets(market_type='SPREAD')
    totals_raw = client.get_nba_markets(market_type='TOTAL')
    moneys_raw = client.get_nba_markets(market_type='MONEY')

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

    # Find the event ID for this game by matching teams
    target_eid = None
    for m in spreads_raw:
        if target_date:
            ts = m.get('startsTs', 0)
            if ts:
                game_date = dt.fromtimestamp(ts / 1000, tz=ET).strftime('%Y-%m-%d')
                if game_date != target_date:
                    continue
        outcomes = m.get('outcomes', [])
        teams = []
        for o in outcomes:
            parts = o['name'].split()
            if parts:
                teams.append(parts[0])
        if len(teams) == 2 and set(teams) == {away_team, home_team}:
            target_eid = m['eventId']
            break

    if not target_eid:
        return {'spreads': [], 'totals': [], 'money': None}

    # Collect all markets for this event
    spread_markets = sorted([m for m in spreads_raw if m['eventId'] == target_eid], key=lambda x: float(x['strike']))
    total_markets = sorted([m for m in totals_raw if m['eventId'] == target_eid], key=lambda x: float(x['strike']))
    money_market = next((m for m in moneys_raw if m['eventId'] == target_eid), None)

    # Pre-limit to 15 closest to median before fetching (avoid 50+ API calls on free tier)
    MAX_PREFETCH = 25
    if len(spread_markets) > MAX_PREFETCH:
        mid_idx = len(spread_markets) // 2
        start = max(0, mid_idx - MAX_PREFETCH // 2)
        spread_markets = spread_markets[start:start + MAX_PREFETCH]
    if len(total_markets) > MAX_PREFETCH:
        mid_idx = len(total_markets) // 2
        start = max(0, mid_idx - MAX_PREFETCH // 2)
        total_markets = total_markets[start:start + MAX_PREFETCH]

    # Fetch order books in parallel, then trim to 9 centered on highest liquidity
    from concurrent.futures import ThreadPoolExecutor
    all_markets = spread_markets + total_markets + ([money_market] if money_market else [])
    market_ids = [m['marketId'] for m in all_markets]
    books = {}
    def _fetch_book(mid):
        try:
            return mid, client.get_book(mid)
        except:
            return mid, None
    with ThreadPoolExecutor(max_workers=8) as ex:
        for mid, book in ex.map(lambda mid: _fetch_book(mid), market_ids):
            books[mid] = book

    # Process spreads
    spreads_out = []
    for m in spread_markets:
        book = books.get(m['marketId'])
        strike = float(m['strike'])
        outcomes = m.get('outcomes', [])
        home_oid = away_oid = None
        for o in outcomes:
            if o['name'].startswith(home_team):
                home_oid = o['outcomeId']
            else:
                away_oid = o['outcomeId']

        away_bid, away_bid_qty = _best_bid(book, away_oid)
        home_price = round(1 - away_bid, 4) if away_bid else None
        home_fill = round(home_price * away_bid_qty / 100) if home_price and away_bid_qty else 0

        home_bid, home_bid_qty = _best_bid(book, home_oid)
        away_price = round(1 - home_bid, 4) if home_bid else None
        away_fill = round(away_price * home_bid_qty / 100) if away_price and home_bid_qty else 0

        spreads_out.append({
            'strike': strike,
            'home_price': home_price, 'home_fill': home_fill,
            'away_price': away_price, 'away_fill': away_fill,
        })

    # Process totals
    totals_out = []
    for m in total_markets:
        book = books.get(m['marketId'])
        strike = float(m['strike'])
        outcomes = m.get('outcomes', [])
        over_oid = under_oid = None
        for o in outcomes:
            if 'Over' in o['name']:
                over_oid = o['outcomeId']
            elif 'Under' in o['name']:
                under_oid = o['outcomeId']

        under_bid, under_bid_qty = _best_bid(book, under_oid)
        over_price = round(1 - under_bid, 4) if under_bid else None
        over_fill = round(over_price * under_bid_qty / 100) if over_price and under_bid_qty else 0

        over_bid, over_bid_qty = _best_bid(book, over_oid)
        under_price = round(1 - over_bid, 4) if over_bid else None
        under_fill = round(under_price * over_bid_qty / 100) if under_price and over_bid_qty else 0

        totals_out.append({
            'strike': strike,
            'over_price': over_price, 'over_fill': over_fill,
            'under_price': under_price, 'under_fill': under_fill,
        })

    # Trim to 9 lines centered on a target strike
    def _trim_centered(lines, center_strike, n=9):
        if len(lines) <= n:
            return lines
        # Find the line closest to center_strike
        center_idx = 0
        best_dist = 9999
        for i, l in enumerate(lines):
            dist = abs(l['strike'] - center_strike)
            if dist < best_dist:
                best_dist = dist
                center_idx = i
        half = n // 2
        start = max(0, center_idx - half)
        end = start + n
        if end > len(lines):
            end = len(lines)
            start = max(0, end - n)
        return lines[start:end]

    # Center on provided strike (from odds summary primary) or fall back to highest liquidity
    if center_spread is not None:
        spreads_out = _trim_centered(spreads_out, center_spread)
    elif spreads_out:
        best_liq, best_strike = -1, spreads_out[len(spreads_out)//2]['strike']
        for s in spreads_out:
            t = (s.get('away_fill') or 0) + (s.get('home_fill') or 0)
            if t > best_liq: best_liq, best_strike = t, s['strike']
        spreads_out = _trim_centered(spreads_out, best_strike)

    if center_total is not None:
        totals_out = _trim_centered(totals_out, center_total)
    elif totals_out:
        best_liq, best_strike = -1, totals_out[len(totals_out)//2]['strike']
        for t in totals_out:
            tl = (t.get('over_fill') or 0) + (t.get('under_fill') or 0)
            if tl > best_liq: best_liq, best_strike = tl, t['strike']
        totals_out = _trim_centered(totals_out, best_strike)

    # Process moneyline
    money_out = None
    if money_market:
        ml_book = None
        try:
            ml_book = client.get_book(money_market['marketId'])
        except:
            pass
        money_out = {}
        for o in money_market.get('outcomes', []):
            oname = o['name'].strip()
            other_oid = [x['outcomeId'] for x in money_market['outcomes'] if x['outcomeId'] != o['outcomeId']]
            if other_oid:
                other_bid, other_qty = _best_bid(ml_book, other_oid[0])
                ask = round(1 - other_bid, 4) if other_bid else None
                fillable = round(ask * other_qty / 100) if ask and other_qty else 0
            else:
                ask = None
                fillable = 0

            if oname == home_team:
                money_out['home_price'] = ask
                money_out['home_fill'] = fillable
            else:
                money_out['away_price'] = ask
                money_out['away_fill'] = fillable

    return {'spreads': spreads_out, 'totals': totals_out, 'money': money_out}


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
