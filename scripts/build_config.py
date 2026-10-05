#!/usr/bin/env python3
"""Regenerate data/config.json from the Cosmos chain registry.

Lists every Noble IBC counterparty (plus XPLA via the Cosmos Hub), derives the USDC.n and USDC.inj IBC denoms,
the Noble/Injective escrow addresses, channel ids and REST endpoints the dashboard and snapshot.py use.
Run: python3 scripts/build_config.py   (needs git; clones the registry sparsely into a temp dir)
"""
import json, os, sys, hashlib, datetime, subprocess, tempfile, urllib.request, urllib.parse, concurrent.futures as cf

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, 'data', 'config.json')
REGISTRY = 'https://github.com/cosmos/chain-registry.git'

def clone_registry():
    d = tempfile.mkdtemp(prefix='chain-registry-')
    subprocess.run(['git', 'clone', '--depth', '1', '--filter=blob:none', '--sparse', '--quiet', REGISTRY, d], check=True)
    subprocess.run(['git', '-C', d, 'sparse-checkout', 'set', '--no-cone', '/_IBC/*', '/*/chain.json', '/*/assetlist.json'], check=True, capture_output=True)
    return d

ROOT = os.environ.get('CHAIN_REGISTRY_DIR') or clone_registry()
IBC = os.path.join(ROOT, '_IBC')

def load(p):
    with open(p) as f: return json.load(f)

def ibc_hash(path, base):
    return 'ibc/' + hashlib.sha256(f'{path}/{base}'.encode()).hexdigest().upper()

def pick_channel(doc, origin):
    """Return (origin_channel, counterparty_name, counterparty_channel, note) for the transfer channel from origin."""
    c1, c2 = doc['chain_1'], doc['chain_2']
    if c1['chain_name'] == origin: o, cp = 'chain_1', 'chain_2'
    else: o, cp = 'chain_2', 'chain_1'
    chans = [c for c in doc.get('channels', []) if c[o].get('port_id') == 'transfer' and c[cp].get('port_id') == 'transfer']
    note = ''
    if not chans: return None
    def score(c):
        t = c.get('tags', {}) or {}
        s = 0
        if t.get('status') == 'live': s += 4
        if t.get('preferred'): s += 2
        if t.get('status') in ('killed', 'upcoming'): s -= 10
        return s
    chans.sort(key=score, reverse=True)
    if len(chans) > 1: note = f'{len(chans)} transfer channels in registry; using {chans[0][o]["channel_id"]}'
    c = chans[0]
    return c[o]['channel_id'], doc[cp]['chain_name'], c[cp]['channel_id'], note, (c.get('tags') or {}).get('status')

def fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={'User-Agent': 'usdc-migration-dashboard/0.1', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()

def rest_endpoints(chain):
    eps = [a['address'].rstrip('/') for a in chain.get('apis', {}).get('rest', [])]
    return [e for e in eps if e.startswith('http')]

def query_supply(eps, denom, max_try=6):
    """Try endpoints until one answers. Returns (amount:int|None, endpoint, error)."""
    errs = []
    for ep in eps[:max_try]:
        for path in (f'/cosmos/bank/v1beta1/supply/by_denom?denom={urllib.parse.quote(denom, safe="")}',
                     f'/cosmos/bank/v1beta1/supply/{urllib.parse.quote(denom, safe="")}'):
            try:
                st, body = fetch(ep + path)
                d = json.loads(body)
                amt = d.get('amount', {}).get('amount')
                if amt is not None:
                    return int(amt), ep, None
                errs.append(f'{ep}: no amount field')
            except Exception as e:
                errs.append(f'{ep}: {type(e).__name__} {str(e)[:60]}')
                break  # endpoint dead, try next endpoint
    return None, None, '; '.join(errs[:3])

INJ_USDC = 'erc20:0xa00C59fF5a080D2b954d0c75e46E22a0c371235a'

# --- bech32 (BIP173) ---
CHARSET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
def _polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk
def _hrp_expand(hrp): return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]
def _convertbits(data, frombits, tobits, pad=True):
    acc = 0; bits = 0; ret = []; maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value; bits += frombits
        while bits >= tobits:
            bits -= tobits; ret.append((acc >> bits) & maxv)
    if pad and bits: ret.append((acc << (tobits - bits)) & maxv)
    return ret
def bech32_encode(hrp, data20):
    data = _convertbits(data20, 8, 5)
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + '1' + ''.join([CHARSET[d] for d in data + checksum])
def escrow_address(hrp, channel):
    h = hashlib.sha256(b'ics20-1' + b'\x00' + f'transfer/{channel}'.encode()).digest()[:20]
    return bech32_encode(hrp, h)

def ibc_file(a, b):
    for n in (f'{a}-{b}.json', f'{b}-{a}.json'):
        p = os.path.join(IBC, n)
        if os.path.exists(p): return p
    return None

def registry_usdc_assets(chain):
    """USDC-ish assets in the chain's assetlist with their traces, for cross-checking denoms."""
    p = os.path.join(ROOT, chain, 'assetlist.json')
    out = []
    if not os.path.exists(p): return out
    for x in load(p).get('assets', []):
        if 'usdc' in x.get('symbol', '').lower():
            tr = x.get('traces') or []
            last = tr[-1] if tr else {}
            out.append({'symbol': x['symbol'], 'base': x['base'], 'from': last.get('counterparty', {}).get('chain_name'),
                        'from_denom': last.get('counterparty', {}).get('base_denom')})
    return out

def main():
    hub_inj = load(ibc_file('cosmoshub', 'injective'))
    hub_to_inj = pick_channel(hub_inj, 'cosmoshub')  # (hub_ch, 'injective', inj_ch, note, status)
    hub_noble = load(ibc_file('cosmoshub', 'noble'))
    hub_to_noble = pick_channel(hub_noble, 'cosmoshub')

    chains = []
    noble_files = sorted(f for f in os.listdir(IBC) if f.endswith('.json') and 'noble' in f[:-5].split('-'))
    names = []
    for f in noble_files:
        doc = load(os.path.join(IBC, f))
        pc = pick_channel(doc, 'noble')
        if not pc:
            names.append((f[:-5].replace('noble-', '').replace('-noble', ''), None, doc)); continue
        names.append((pc[1], pc, doc))
    # XPLA: no direct Noble channel; routes via Cosmos Hub
    names.append(('xpla', None, None))

    for name, pc, doc in names:
        row = {'name': name, 'gaps': []}
        cj = os.path.join(ROOT, name, 'chain.json')
        if os.path.exists(cj):
            ch = load(cj)
            row.update({'pretty': ch.get('pretty_name', name), 'chain_id': ch.get('chain_id'), 'bech32': ch.get('bech32_prefix'),
                        'status': ch.get('status'), 'network_type': ch.get('network_type'),
                        'rest': [f'https://rest.cosmos.directory/{name}'] + rest_endpoints(ch),
                        'explorer': next((e.get('url') for e in ch.get('explorers', []) if e.get('url')), None)})
        else:
            row.update({'pretty': name, 'chain_id': None, 'rest': []})
            row['gaps'].append('No chain.json in the chain registry (not a Cosmos SDK REST chain); balances cannot be queried this way.')
        # --- Noble leg ---
        if name == 'xpla':
            hub_ch_to_noble, _, noble_ch_to_hub, _, _ = hub_to_noble
            xpla_hub = load(ibc_file('xpla', 'cosmoshub'))
            xpla_to_hub = pick_channel(xpla_hub, 'xpla')
            row['route'] = 'via-cosmoshub'
            row['noble_channel'] = None
            row['usdc_n_path'] = f'transfer/{xpla_to_hub[0]}/transfer/{hub_ch_to_noble}/uusdc'
            row['usdc_n_denom'] = ibc_hash(f'transfer/{xpla_to_hub[0]}/transfer/{hub_ch_to_noble}', 'uusdc')
            row['noble_escrow'] = None
            row['gaps'].append('XPLA has no direct Noble channel; USDC.n and USDC.inj are measured on the multi-hop path through the Cosmos Hub. No per-chain escrow history exists for a multi-hop route, so 24h/7d/since-Sept-1 come from the snapshot store only.')
            row['xpla_to_hub'] = xpla_to_hub[0]
        elif pc:
            noble_ch, _, cp_ch, note, status = pc
            row['route'] = 'direct'
            row['noble_channel'] = noble_ch; row['chain_channel_to_noble'] = cp_ch
            row['noble_channel_status_registry'] = status
            row['usdc_n_path'] = f'transfer/{cp_ch}/uusdc'
            row['usdc_n_denom'] = ibc_hash(f'transfer/{cp_ch}', 'uusdc')
            row['noble_escrow'] = escrow_address('noble', noble_ch)
            if note: row['gaps'].append('Registry lists ' + note.split(';')[0].strip() + ' to Noble; only the preferred/live one is tracked.')
        else:
            row['route'] = 'direct'; row['noble_channel'] = None; row['usdc_n_denom'] = None
            row['gaps'].append('Registry IBC file with Noble has no transfer channel entry.')
        # --- Injective leg ---
        inj = ibc_file(name, 'injective') if name != 'injective' else None
        if name == 'xpla':
            hub_ch, _, inj_ch_to_hub, _, _ = hub_to_inj
            row['injective_registry_file'] = os.path.basename(inj) if inj else None
            row['usdc_inj_path'] = f'transfer/{row["xpla_to_hub"]}/transfer/{hub_ch}/{INJ_USDC}'
            row['usdc_inj_denom'] = ibc_hash(f'transfer/{row["xpla_to_hub"]}/transfer/{hub_ch}', INJ_USDC)
            row['injective_channel'] = None; row['chain_channel_to_injective'] = None; row['injective_escrow'] = None
            if inj:
                d = load(inj); p2 = pick_channel(d, 'injective')
                if p2:
                    row['injective_channel_direct'] = p2[0]; row['chain_channel_to_injective_direct'] = p2[2]
                    row['usdc_inj_denom_direct'] = ibc_hash(f'transfer/{p2[2]}', INJ_USDC)
        elif inj:
            d = load(inj); p2 = pick_channel(d, 'injective')
            row['injective_registry_file'] = os.path.basename(inj)
            if p2:
                inj_ch, _, cp_ch2, note2, status2 = p2
                row['injective_channel'] = inj_ch; row['chain_channel_to_injective'] = cp_ch2
                row['injective_channel_status_registry'] = status2
                row['usdc_inj_path'] = f'transfer/{cp_ch2}/{INJ_USDC}'
                row['usdc_inj_denom'] = ibc_hash(f'transfer/{cp_ch2}', INJ_USDC)
                row['injective_escrow'] = escrow_address('inj', inj_ch)
                if note2: row['gaps'].append('Registry lists ' + note2.split(';')[0].strip() + ' to Injective; only the preferred/live one is tracked.')
            else:
                row['injective_channel'] = None; row['usdc_inj_denom'] = None
                row['gaps'].append('Registry IBC file with Injective exists but has no transfer channel entry.')
        else:
            row['injective_registry_file'] = None; row['injective_channel'] = None; row['usdc_inj_denom'] = None
        # registry cross-check
        row['usdc_inj_listed'] = bool(row.get('usdc_inj_denom')) and any(a['base'] == row['usdc_inj_denom'] for a in registry_usdc_assets(name))
        row['registry_usdc_assets'] = registry_usdc_assets(name)
        reg_n = [a for a in row['registry_usdc_assets'] if a['from'] == 'noble' and a['from_denom'] == 'uusdc']
        reg_i = [a for a in row['registry_usdc_assets'] if a['from_denom'] == INJ_USDC]
        if row.get('usdc_n_denom') and reg_n and reg_n[0]['base'] != row['usdc_n_denom'] and name != 'xpla':
            row['gaps'].append(f"Assetlist USDC.n denom ({reg_n[0]['base'][:14]}...) differs from the denom derived from the preferred channel; registry may list a different channel.")
        if row.get('usdc_inj_denom') and reg_i and reg_i[0]['base'] != row['usdc_inj_denom'] and name != 'xpla':
            row['gaps'].append(f"Assetlist USDC.inj denom ({reg_i[0]['base'][:14]}...) differs from the denom derived from the preferred channel.")
        if row.get('usdc_inj_denom') and not reg_i and name != 'injective' and not any(a['base'] == row['usdc_inj_denom'] for a in row['registry_usdc_assets']):
            row['gaps'].append('USDC.inj is not yet listed in this chain\'s registry assetlist (wallets/explorers may show it as an unknown IBC denom).')
        chains.append(row)

    cfg = {
        'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'registry_commit': subprocess.run(['git', '-C', ROOT, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip(),
        'inj_usdc_base': INJ_USDC,
        'noble_rest': ['https://rest.cosmos.directory/noble', 'https://noble-api.polkachu.com', 'https://rest.lavenderfive.com:443/noble'] + rest_endpoints(load(os.path.join(ROOT, 'noble', 'chain.json'))),
        'injective_rest': ['https://public.stakewolle.com/cosmos/injective/rest', 'https://rest.cosmos.directory/injective', 'https://injective-api.polkachu.com'] + rest_endpoints(load(os.path.join(ROOT, 'injective', 'chain.json'))),
        'hub_channel_to_injective': hub_to_inj[0], 'hub_channel_to_noble': hub_to_noble[0],
        'chains': chains,
    }
    # dedupe endpoint lists
    for k in ('noble_rest', 'injective_rest'): cfg[k] = list(dict.fromkeys(cfg[k]))
    for c in chains:
        if c.get('rest'): c['rest'] = list(dict.fromkeys(c['rest']))
    # retired / deprecated chains: shown but excluded from the tracked set (edit data/retired.json)
    rp = os.path.join(REPO, 'data', 'retired.json')
    cfg['retired'] = json.load(open(rp)) if os.path.exists(rp) else {}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(cfg, open(OUT, 'w'), indent=1)
    print('wrote', OUT)
    print(len(chains), 'chains;', sum(1 for c in chains if c.get('usdc_inj_denom')), 'with an Injective channel in registry')
    # sanity: escrow address check


if __name__ == '__main__':
    main()
