#!/usr/bin/env python3
"""sync_global.py v2.0 — Sincroniza com filtro de altura e peer explícito.

Exemplos:
  # Sincronizar SÓ do 61LTR37, exigindo altura 5026:
  python sync_global.py --only --peer http://100.70.107.15:5000 --target-height 5026

  # Testar sem baixar:
  python sync_global.py --only --peer http://100.70.107.15:5000 --dry-run

  # Modo antigo (maior altura):
  python sync_global.py
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print('[X] Falta: pip install requests'); sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'brn_v2_chain.db'
ENV_FILE = BASE_DIR / 'brn_network.env'

DEFAULT_BOOTSTRAP_PEERS = [
    'http://100.70.107.15:5000',  # desktop-61ltr37 (ORIGEM, 5026)
    'http://100.64.151.67:5000',  # Filipe (5026)
]


def log(msg: str):
    print(msg, flush=True)


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def normalize_url(url: str) -> Optional[str]:
    url = url.strip()
    if not url:
        return None
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url.rstrip('/')


def discover_from_env(env: dict) -> list[str]:
    urls = []
    raw = env.get('BRN_BOOTSTRAP_PEERS', '')
    for item in raw.split(','):
        item = item.strip()
        if not item or ':' not in item:
            continue
        ip = item.rsplit(':', 1)[0]
        if ip:
            urls.append(f'http://{ip}:5000')
    return urls


def discover_from_tailscale() -> list[str]:
    import subprocess
    try:
        r = subprocess.run(['tailscale', 'status', '--json'],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        urls = []
        for p in (data.get('Peer') or {}).values():
            if not p.get('Online'):
                continue
            for ip in (p.get('TailscaleIPs') or []):
                if ip.startswith('100.'):
                    urls.append(f'http://{ip}:5000')
                    break
        return urls
    except Exception:
        return []


def dedupe(urls: list[str]) -> list[str]:
    seen = set()
    return [u for u in urls if u and not (u in seen or seen.add(u))]


def fetch_status(url: str, timeout: float = 30.0) -> Optional[dict]:
    try:
        r = requests.get(f'{url}/api/status', timeout=timeout,
                         proxies={'http': None, 'https': None},
                         headers={'User-Agent': 'brn-sync-global/2.0'})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def probe_peer(url: str, timeout: float = 30.0) -> dict:
    result = {'url': url, 'reachable': False, 'height': -1,
              'tip_hash': '', 'utxos': 0, 'version': '?', 'error': ''}
    status = fetch_status(url, timeout=timeout)
    if not status:
        result['error'] = 'sem resposta'
        return result
    result['reachable'] = True
    result['height'] = int(status.get('height', -1) or -1)
    result['tip_hash'] = status.get('tip_hash', '')
    result['utxos'] = int(status.get('utxos', 0) or 0)
    result['version'] = status.get('version', '?')
    return result


def fetch_blocks(url: str, start: int, batch: int = 100,
                 timeout: float = 120.0, retries: int = 3) -> Optional[list]:
    full_url = f'{url}/api/blocks?start={start}&limit={batch}'
    for attempt in range(retries):
        try:
            r = requests.get(full_url, timeout=timeout,
                             proxies={'http': None, 'https': None},
                             headers={'User-Agent': 'brn-sync-global/2.0'})
            if r.status_code == 200:
                return r.json().get('blocks', [])
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(3)
    return None


def download_chain(url: str, remote_height: int, local_height: int,
                   batch: int = 100, timeout: float = 120.0) -> list[dict]:
    all_blocks = []
    total = remote_height + 1
    t0 = time.time()

    for start in range(local_height + 1, remote_height + 1, batch):
        end = min(start + batch, remote_height + 1)
        actual = end - start

        blocks = fetch_blocks(url, start, actual, timeout=timeout)
        if not blocks:
            log(f'  [X] Lote {start}..{end-1} falhou após 3 tentativas')
            return all_blocks

        all_blocks.extend(blocks)
        elapsed = time.time() - t0
        downloaded = local_height + len(all_blocks) + 1
        pct = (downloaded / total) * 100
        rate = len(all_blocks) / max(0.1, elapsed)
        eta = (remote_height - downloaded) / max(0.1, rate) if rate > 0 else 0
        log(f'  [{downloaded}/{total}] {pct:.1f}% | {rate:.0f} bl/s | ETA {eta:.0f}s')

        if len(blocks) < actual:
            break

    return all_blocks


def import_chain(blocks: list[dict]) -> tuple[int, int]:
    from db import ChainDB
    db = ChainDB(str(DB_PATH))
    before = db.height()
    imported = 0

    for block in blocks:
        h = block.get('height')
        if h is None or db.get_block(h) is not None:
            continue
        try:
            db.add_block(block)
            for i, tx in enumerate(block.get('transactions', [])):
                db.apply_tx(tx, h, coinbase=(i == 0))
            imported += 1
        except Exception as e:
            log(f'  [X] Erro no bloco #{h}: {e}')
            break

    after = db.height()
    db.close()
    return imported, after


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--peer', action='append', default=[])
    ap.add_argument('--only', action='store_true',
                    help='Usa SÓ --peer (ignora env/tailscale/defaults)')
    ap.add_argument('--target-height', type=int, default=None,
                    help='Só sincroniza de peers com esta altura EXATA')
    ap.add_argument('--batch', type=int, default=100)
    ap.add_argument('--timeout', type=float, default=120.0)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-tailscale', action='store_true')
    args = ap.parse_args()

    log('=' * 70)
    log(' SYNC GLOBAL v2.0')
    if args.only:
        log(' MODO: --only (só --peer explícito)')
    if args.target_height is not None:
        log(f' FILTRO: altura == {args.target_height}')
    log('=' * 70)

    # ===== 1. Descobrir =====
    sources = []
    if args.peer:
        urls = [normalize_url(u) for u in args.peer]
        urls = [u for u in urls if u]
        log(f'\n[1] CLI (--peer): {len(urls)}')
        sources.extend(urls)

    if not args.only:
        env = load_env()
        urls = discover_from_env(env)
        log(f'    brn_network.env: {len(urls)}')
        sources.extend(urls)

        for u in DEFAULT_BOOTSTRAP_PEERS:
            sources.append(normalize_url(u))

        if not args.no_tailscale:
            urls = discover_from_tailscale()
            log(f'    Tailscale: {len(urls)}')
            sources.extend(urls)

    all_urls = dedupe(sources)
    log(f'\n    Total únicos: {len(all_urls)}')
    for u in all_urls:
        log(f'      - {u}')

    if not all_urls:
        log('\n[X] Nenhum peer.')
        return 1

    # ===== 2. Testar =====
    log(f'\n[2] Testando {len(all_urls)} peers...')
    probed = []
    for u in all_urls:
        log(f'  {u}...', end=' ')
        r = probe_peer(u, timeout=args.timeout)
        probed.append(r)
        if r['reachable']:
            log(f'OK — altura={r["height"]}')
        else:
            log(f'FALHOU ({r["error"]})')

    # ===== 3. Selecionar =====
    try:
        from db import ChainDB
        db = ChainDB(str(DB_PATH))
        local_height = db.height()
        db.close()
    except Exception:
        local_height = -1

    reachable = [p for p in probed if p['reachable']]

    # Filtro por altura EXATA
    if args.target_height is not None:
        exact = [p for p in reachable if p['height'] == args.target_height]
        log(f'\n[3] Altura local: {local_height}')
        log(f'    Peers com altura {args.target_height}: {len(exact)}')
        for p in exact:
            log(f'      - {p["url"]} (altura={p["height"]})')
        if not exact:
            log(f'\n[X] Nenhum peer tem altura {args.target_height}.')
            return 1
        reachable = exact
    else:
        reachable.sort(key=lambda p: p['height'], reverse=True)
        log(f'\n[3] Altura local: {local_height}')
        log(f'    Peers alcançáveis: {len(reachable)}')
        for p in reachable:
            mark = '' if p['height'] <= local_height else '  [MAIOR]'
            log(f'      - {p["url"]:<40} altura={p["height"]}{mark}')

    # Se local já >= target, nada a fazer
    if args.target_height is not None and local_height >= args.target_height:
        log(f'\n[OK] Já sincronizado (local={local_height} >= target={args.target_height})')
        return 0

    valid = [p for p in reachable if p['height'] > local_height]
    if not valid:
        log('\n[OK] Já sincronizado.')
        return 0

    if args.dry_run:
        log('\n[DRY-RUN] Sairia agora.')
        return 0

    # ===== 4. Sincronizar =====
    log(f'\n[4] Baixando (primeiro = mais alto)...')
    for peer in valid:
        log(f'\n--- {peer["url"]} (altura={peer["height"]}) ---')
        blocks = download_chain(peer['url'], peer['height'],
                                local_height, batch=args.batch,
                                timeout=args.timeout)
        if not blocks:
            log('  [X] Nenhum bloco')
            continue

        log(f'\n[*] Importando {len(blocks)} blocos...')
        imported, final_h = import_chain(blocks)
        log(f'[OK] {imported} importados. Altura final: {final_h}')

        if final_h >= peer['height']:
            log(f'\n[✓] Sincronizado via {peer["url"]}')
            return 0

    log('\n[X] Falhou.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
