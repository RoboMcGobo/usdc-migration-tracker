import json, os, sys, urllib.request, urllib.parse, datetime, concurrent.futures as cf, time
"""Take today's snapshot of the USDC.n to USDC.inj migration and append it to data/snapshots.json.

For every chain in data/config.json: on-chain USDC.n and USDC.inj supply, Injective channel state on both ends,
Noble escrow (now, 24h, 7d, Sept 1) and Injective escrow (now, 24h, 7d) from public archive nodes.
One entry per UTC day; re-running on the same day overwrites that day's entry.
Run: python3 scripts/snapshot.py
"""
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = json.load(open(os.path.join(REPO, 'data', 'config.json')))
STORE = os.path.join(REPO, 'data', 'snapshots.json')
UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC)
SEP1 = datetime.datetime(2026, 9, 1, tzinfo=UTC)
TARGETS = {'24h': NOW - datetime.timedelta(days=1), '7d': NOW - datetime.timedelta(days=7), 'sep1': SEP1}

def get(url, h=None, t=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'usdc-migration-dashboard', **({'x-cosmos-block-height': str(h)} if h else {})})
    with urllib.request.urlopen(req, timeout=t) as r: return json.loads(r.read())

def first(eps, path, h=None, want=None, tries=1):
    errs = []
    for ep in eps:
        for _ in range(tries):
            try:
                d = get(ep + path, h)
                if want and want(d) is None: errs.append(f'{ep}: unexpected body'); continue
                return d, ep, None
            except Exception as e:
                errs.append(f'{ep}: {type(e).__name__} {str(e)[:50]}')
    return None, None, errs

def ts(s): return datetime.datetime.fromisoformat(s[:26].rstrip('Z') + '+00:00')

def header(eps, h):
    d, ep, e = first(eps, f'/cosmos/base/tendermint/v1beta1/blocks/{h}' if h else '/cosmos/base/tendermint/v1beta1/blocks/latest')
    return (int(d['block']['header']['height']), ts(d['block']['header']['time'])) if d else (None, None)

def height_at(eps, target):
    """Binary search block height whose time <= target (archive node needed for old headers)."""
    H, T = header(eps, None)
    if H is None: return None
    if target >= T: return H
    lo_h, lo_t = header(eps, max(1, H - 200000))
    if lo_h is None: return None
    # extrapolate with average block time then refine
    bt = (T - lo_t).total_seconds() / (H - lo_h)
    guess = H - int((T - target).total_seconds() / bt)
    for _ in range(12):
        gh, gt = header(eps, max(1, guess))
        if gh is None: return None
        diff = (gt - target).total_seconds()
        if abs(diff) < 60: return gh
        guess = gh - int(diff / bt)
    return gh

def supply(eps, denom):
    d, ep, e = first(eps, f'/cosmos/bank/v1beta1/supply/by_denom?denom={urllib.parse.quote(denom, safe="")}', want=lambda d: d.get('amount', {}).get('amount'))
    if d: return int(d['amount']['amount']), ep, None
    # fallback: full list
    d2, ep2, e2 = first(eps, '/cosmos/bank/v1beta1/supply?pagination.limit=5000', want=lambda d: d.get('supply'))
    if d2:
        m = [s for s in d2['supply'] if s['denom'] == denom]
        return (int(m[0]['amount']) if m else 0), ep2 + ' (full supply list)', None
    return None, None, (e or []) + (e2 or [])

def balance(eps, addr, denom, h=None):
    d, ep, e = first(eps, f'/cosmos/bank/v1beta1/balances/{addr}/by_denom?denom={urllib.parse.quote(denom, safe="")}', h, want=lambda d: d.get('balance', {}).get('amount'), tries=2)
    return (int(d['balance']['amount']), ep, None) if d else (None, None, e)

def channel_state(eps, channel):
    d, ep, e = first(eps, f'/ibc/core/channel/v1/channels/{channel}/ports/transfer', want=lambda d: d.get('channel'))
    if d: return d['channel']['state'], d['channel'].get('counterparty', {}).get('channel_id'), None
    return None, None, e

def main():
    out = {'fetched_at': NOW.isoformat(timespec='seconds'), 'heights': {}, 'chains': {}, 'totals': {}}
    noble = cfg['noble_rest']; inj = cfg['injective_rest']
    # heights
    for name, eps in (('noble', noble), ('injective', inj)):
        out['heights'][name] = {}
        for k, t in TARGETS.items():
            try: out['heights'][name][k] = height_at(eps, t)
            except Exception as e: out['heights'][name][k] = None
        print(name, out['heights'][name])
    # totals
    d, ep, e = first(noble, '/ibc/apps/transfer/v1/denoms/uusdc/total_escrow'); out['totals']['noble_total_escrow'] = int(d['amount']['amount']) if d else None
    d, ep, e = first(noble, '/cosmos/bank/v1beta1/supply/by_denom?denom=uusdc'); out['totals']['noble_usdc_supply'] = int(d['amount']['amount']) if d else None
    d, ep, e = first(inj, f'/cosmos/bank/v1beta1/supply/by_denom?denom={urllib.parse.quote(cfg["inj_usdc_base"], safe="")}'); out['totals']['injective_usdc_supply'] = int(d['amount']['amount']) if d else None
    d, ep, e = first(inj, f'/ibc/apps/transfer/v1/denoms/{urllib.parse.quote(cfg["inj_usdc_base"], safe="")}/total_escrow'); out['totals']['injective_total_escrow'] = int(d['amount']['amount']) if d else None
    for k in TARGETS:
        h = out['heights']['noble'].get(k)
        if h:
            d, ep, e = first(noble, '/ibc/apps/transfer/v1/denoms/uusdc/total_escrow', h); out['totals'][f'noble_total_escrow_{k}'] = int(d['amount']['amount']) if d else None
        h = out['heights']['injective'].get(k)
        if h:
            d, ep, e = first(inj, f'/cosmos/bank/v1beta1/supply/by_denom?denom={urllib.parse.quote(cfg["inj_usdc_base"], safe="")}', h); out['totals'][f'injective_usdc_supply_{k}'] = int(d['amount']['amount']) if d else None
    print(out['totals'])

    def work(c):
        r = {'errors': {}}
        eps = c.get('rest') or []
        if c.get('usdc_n_denom') and eps:
            a, ep, e = supply(eps, c['usdc_n_denom']); r['usdc_n'] = a; r['usdc_n_src'] = ep
            if a is None: r['errors']['usdc_n'] = e[:4]
        if c.get('usdc_inj_denom') and eps:
            a, ep, e = supply(eps, c['usdc_inj_denom']); r['usdc_inj'] = a; r['usdc_inj_src'] = ep
            if a is None: r['errors']['usdc_inj'] = e[:4]
        if c.get('chain_channel_to_injective') and eps:
            st, cp, e = channel_state(eps, c['chain_channel_to_injective']); r['inj_channel_state_chain_side'] = st; r['inj_channel_counterparty'] = cp
            if st is None: r['errors']['chain_side_channel'] = e[:3]
        if c.get('injective_channel'):
            st, cp, e = channel_state(inj, c['injective_channel']); r['inj_channel_state_injective_side'] = st
            if st is None: r['errors']['injective_side_channel'] = e[:3]
        if c.get('noble_escrow'):
            r['noble_escrow'] = {}
            for k, h in [('now', None)] + list(out['heights']['noble'].items()):
                if k != 'now' and not h: continue
                a, ep, e = balance(noble, c['noble_escrow'], 'uusdc', h); r['noble_escrow'][k] = a
                if a is None: r['errors'][f'noble_escrow_{k}'] = e[:3]
        if c.get('injective_escrow'):
            r['injective_escrow'] = {}
            for k, h in [('now', None)] + list(out['heights']['injective'].items()):
                if k != 'now' and not h: continue
                a, ep, e = balance(inj, c['injective_escrow'], cfg['inj_usdc_base'], h); r['injective_escrow'][k] = a
                if a is None: r['errors'][f'injective_escrow_{k}'] = e[:3]
        return c['name'], r
    with cf.ThreadPoolExecutor(8) as ex:
        for name, r in ex.map(work, cfg['chains']):
            out['chains'][name] = r
            print(f"{name:<14} n={r.get('usdc_n')} inj={r.get('usdc_inj')} ch={r.get('inj_channel_state_chain_side')}/{r.get('inj_channel_state_injective_side')} nesc={r.get('noble_escrow')} iesc={r.get('injective_escrow')} errs={list(r['errors'].keys())}")
    # fold into the store: compact per-chain shape the dashboard reads
    snap = {'date': NOW.strftime('%Y-%m-%d'), 'at': out['fetched_at'], 'source': 'github-action', 'heights': out['heights'],
            'totals': {k: out['totals'].get(k) for k in ('noble_total_escrow', 'noble_usdc_supply', 'injective_usdc_supply', 'injective_total_escrow')}, 'chains': {}}
    for name, r in out['chains'].items():
        o = {}
        if r.get('usdc_n') is not None: o['usdc_n'] = r['usdc_n']
        if r.get('usdc_inj') is not None: o['usdc_inj'] = r['usdc_inj']
        if r.get('noble_escrow') and r['noble_escrow'].get('now') is not None: o['noble_escrow'] = {'now': r['noble_escrow']['now']}
        if r.get('injective_escrow') and r['injective_escrow'].get('now') is not None: o['injective_escrow'] = {'now': r['injective_escrow']['now']}
        for k in ('inj_channel_state_chain_side', 'inj_channel_state_injective_side'):
            if r.get(k): o[k] = r[k]
        if o: snap['chains'][name] = o
    measured = sum(1 for o in snap['chains'].values() if 'usdc_n' in o or 'noble_escrow' in o)
    if measured < 5 or snap['totals']['noble_total_escrow'] is None:
        print(f'Refusing to write a snapshot with only {measured} measured chains (endpoints down?).', file=sys.stderr); sys.exit(1)
    store = {'version': 1, 'snapshots': {}}
    if os.path.exists(STORE):
        try: store = json.load(open(STORE))
        except Exception: pass
    store.setdefault('snapshots', {})[snap['date']] = snap
    store['updated_at'] = out['fetched_at']
    json.dump(store, open(STORE, 'w'), indent=0, separators=(',', ':'))
    json.dump(out, open(os.path.join(REPO, 'data', 'last_run_full.json'), 'w'), indent=1)
    print(f"wrote {STORE}: {len(store['snapshots'])} days, {measured} chains measured today")

if __name__ == '__main__':
    main()
