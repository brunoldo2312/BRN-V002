#!/usr/bin/env python3
\"\"\"sync_global.py — Sincroniza blockchain de QUALQUER LUGAR DO MUNDO.

Uso:
  python sync_global.py
  python sync_global.py --peer http://1.2.3.4:5000
  python sync_global.py --peers-file peers.txt
  python sync_global.py --tracker http://tracker.brn.network:9000
  python sync_global.py --dry-run
  python sync_global.py --batch 200 --timeout 120
\"\"\"
from __future__ import annotations
import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print('[X] Falta: pip install requests')
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'brn_v2_chain.db'
ENV_FILE = BASE_DIR / 'brn_network.env'

# ============================================================
# BOOTSTRAP PEERS PÚBLICOS
# ============================================================
# Adicione aqui os peers que devem servir como ponto de entrada
# global. Formato: 'http://ip:porta' ou 'https://dominio.com'
DEFAULT_BOOTSTRAP_PEERS = [
    # Exemplo:
    # 'https://peer1.brn.network',
    # 'https://peer2.brn.network',
    # 'http://200.123.45.67:5000',
]

# Tracker público opcional (GET /peers retorna {"peers": [...]})
DEFAULT_TRACKER = ''

# ============================================================
# HELPERS
# ============================================================
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
            env[k.strip()] = v.strip().strip('\"').strip(\"'\")
    return env


def load_peers_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    peers = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            peers.append(line)
    return peers


def normalize_url(url: str) -> Optional[str]:
    url = url.strip()
    if not url:
        return None
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    # Remove barra final
    return url.rstrip('/')


# ============================================================
# DESCOBERTA
# ============================================================
def discover_from_cli(peers_arg: list[str]) -> list[str]:
    urls = []
    for p in peers_arg:
        u = normalize_url(p)
        if u:
            urls.append(u)
    return urls


def discover_from_env(env: dict) -> list[str]:
    urls = []
    raw = env.get('BRN_BOOTSTRAP_PEERS', '')
    for item in raw.split(','):
        item = item.strip()
        if not item or ':' not in item:
            continue
        # env pode ter 'ip:port' (P2P), então usamos porta HTTP padrão
        ip = item.rsplit(':', 1)[0]
        if ip:
            urls.append(f'http://{ip}:5000')
    return urls


def discover_from_tracker(tracker_url: str, timeout: float = 15.0) -> list[str]:
    if not tracker_url:
        return []
    tracker_url = tracker_url.rstrip('/')
    try:
        r = requests.get(f'{tracker_url}/peers', timeout=timeout,
                         proxies={'http': None, 'https': None})
        if r.status_code != 200:
            log(f'  [X] Tracker HTTP {r.status_code}')
            return []
        data = r.json()
        peers = data.get('peers', [])
        urls = []
        for p in peers:
            if isinstance(p, str) and ':' in p:
                ip = p.rsplit(':', 1)[0]
                urls.append(f'http://{ip}:5000')
        log(f'  [OK] Tracker: {len(urls)} peers')
        return urls
    except Exception as e:
        log(f'  [X] Tracker falhou: {e}')
        return []


def discover_from_tailscale() -> list[str]:
    \"\"\"Descobre peers Tailscale (opcional, se tiver).\"\"\"
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
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ============================================================
# TESTE DE PEER
# ============================================================
def fetch_status(url: str, timeout: float = 30.0) -> Optional[dict]:
    try:
        r = requests.get(f'{url}/api/status', timeout=timeout,
                         proxies={'http': None, 'https': None},
                         headers={'User-Agent': 'brn-sync-global/1.0'})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def probe_peer(url: str, timeout: float = 30.0) -> dict:
    result = {
        'url': url,
        'reachable': False,
        'height': -1,
        'tip_hash': '',
        'utxos': 0,
        'version': '?',
        'error': '',
    }
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


# ============================================================
# DOWNLOAD
# ============================================================
def fetch_blocks(url: str, start: int, batch: int = 100,
                 timeout: float = 120.0, retries: int = 3) -> Optional[list]:
    full_url = f'{url}/api/blocks?start={start}&limit={batch}'
    for attempt in range(retries):
        try:
            r = requests.get(full_url, timeout=timeout,
                             proxies={'http': None, 'https': None},
                             headers={'User-Agent': 'brn-sync-global/1.0'})
            if r.status_code == 200:
                data = r.json()
                return data.get('blocks', [])
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
            log(f'  [X] Lote {start}..{end-1} falhou')
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


# ============================================================
# VALIDAÇÃO E IMPORTAÇÃO
# ============================================================
def validate_blocks(blocks: list[dict], start_height: int,
                    expected_prev: str) -> tuple[bool, str]:
    \"\"\"Valida encadeamento antes de importar.\"\"\"
    if not blocks:
        return False, 'lista vazia'

    try:
        from blockchain import block_hash, meets_difficulty
    except ImportError:
        return False, 'blockchain.py sem helpers'

    expected_h = start_height
    prev = expected_prev
    for blk in blocks:
        h = int(blk.get('height', -1))
        if h != expected_h:
            return False, f'altura {h} != {expected_h}'
        if blk.get('prev_hash') != prev:
            return False, f'prev_hash diverge em #{h}'
        calc = block_hash(blk['prev_hash'], blk['merkle'],
                          blk['timestamp'], blk['nonce'], blk['difficulty'])
        if calc != blk.get('hash'):
            return False, f'hash inválido em #{h}'
        if not meets_difficulty(calc, int(blk['difficulty'])):
            return False, f'PoW inválido em #{h}'
        prev = calc
        expected_h += 1
    return True, 'ok'


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


# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(
        description='Sincroniza blockchain de qualquer lugar do mundo')
    ap.add_argument('--peer', action='append', default=[],
                    help='URL do peer (pode repetir)')
    ap.add_argument('--peers-file', type=Path,
                    help='Arquivo com URLs (1 por linha)')
    ap.add_argument('--tracker', default=DEFAULT_TRACKER,
                    help='URL do tracker (GET /peers)')
    ap.add_argument('--batch', type=int, default=100,
                    help='Blocos por requisição (default: 100)')
    ap.add_argument('--timeout', type=float, default=120.0,
                    help='Timeout por requisição em segundos')
    ap.add_argument('--dry-run', action='store_true',
                    help='Só testa peers, não baixa')
    ap.add_argument('--no-tailscale', action='store_true',
                    help='Não tentar Tailscale')
    args = ap.parse_args()

    log('=' * 70)
    log(' SYNC GLOBAL — Blockchain de Qualquer Lugar')
    log('=' * 70)

    # ===== 1. Descobrir peers =====
    log('\n[1] Descobrindo peers...')
    sources = []

    if args.peer:
        urls = discover_from_cli(args.peer)
        log(f'  CLI (--peer): {len(urls)}')
        sources.extend(urls)

    if args.peers_file:
        urls = [normalize_url(u) for u in load_peers_from_file(args.peers_file)]
        urls = [u for u in urls if u]
        log(f'  Arquivo {args.peers_file.name}: {len(urls)}')
        sources.extend(urls)

    env = load_env()
    urls = discover_from_env(env)
    log(f'  brn_network.env: {len(urls)}')
    sources.extend(urls)

    # Bootstrap padrão (editado manualmente no código)
    for u in DEFAULT_BOOTSTRAP_PEERS:
        sources.append(normalize_url(u))

    if args.tracker:
        urls = discover_from_tracker(args.tracker, timeout=args.timeout)
        sources.extend(urls)

    if not args.no_tailscale:
        urls = discover_from_tailscale()
        if urls:
            log(f'  Tailscale: {len(urls)}')
        sources.extend(urls)

    all_urls = dedupe([u for u in sources if u])
    log(f'\n  Total únicos: {len(all_urls)}')
    for u in all_urls:
        log(f'    - {u}')

    if not all_urls:
        log('\n[X] Nenhum peer descoberto.')
        log('    Use: python sync_global.py --peer http://ip:5000')
        log('    Ou configure DEFAULT_BOOTSTRAP_PEERS no código.')
        return 1

    # ===== 2. Testar peers =====
    log(f'\n[2] Testando {len(all_urls)} peers...')
    probed = []
    for u in all_urls:
        log(f'  {u}...', end=' ')
        r = probe_peer(u, timeout=args.timeout)
        probed.append(r)
        if r['reachable']:
            log(f'OK — altura={r[\"height\"]}, utxos={r[\"utxos\"]}')
        else:
            log(f'FALHOU ({r[\"error\"]})')

    # ===== 3. Selecionar melhor =====
    local_height = -1
    try:
        from db import ChainDB
        db = ChainDB(str(DB_PATH))
        local_height = db.height()
        db.close()
    except Exception as e:
        log(f'\n[!] Erro lendo DB local: {e}')

    reachable = [p for p in probed if p['reachable']]
    reachable.sort(key=lambda p: p['height'], reverse=True)

    log(f'\n[3] Altura local: {local_height}')
    log(f'    Peers alcançáveis: {len(reachable)}')
    for p in reachable:
        mark = '' if p['height'] <= local_height else '  [MAIOR]'
        log(f'    - {p[\"url\"]:<45} altura={p[\"height\"]}{mark}')

    valid = [p for p in reachable if p['height'] > local_height]
    if not valid:
        log('\n[OK] Já está sincronizado com o melhor peer.')
        return 0

    if args.dry_run:
        log('\n[DRY-RUN] Sairia agora.')
        return 0

    # ===== 4. Tentar cada peer =====
    log('\n[4] Tentando download (maior altura primeiro)...')
    for peer in valid:
        log(f'\n--- Tentando {peer[\"url\"]} (altura={peer[\"height\"]}) ---')

        blocks = download_chain(
            peer['url'], peer['height'], local_height,
            batch=args.batch, timeout=args.timeout
        )

        if not blocks:
            log('  [X] Nenhum bloco baixado')
            continue

        # Validação
        log(f'\n[*] Baixados {len(blocks)} blocos. Validando...')
        try:
            from db import ChainDB
            db = ChainDB(str(DB_PATH))
            expected_h = db.height() + 1
            expected_prev = db.tip_hash()
            db.close()
        except Exception:
            expected_h = local_height + 1
            expected_prev = '0' * 64

        ok, msg = validate_blocks(blocks, expected_h, expected_prev)
        if not ok:
            log(f'  [X] Validação falhou: {msg}')
            continue

        log('  [OK] Validação passou')

        # Importação
        log('\n[*] Importando...')
        imported, final_h = import_chain(blocks)
        log(f'[OK] {imported} blocos importados. Altura final: {final_h}')

        if final_h >= peer['height']:
            log(f'\n[✓] Sincronizado via {peer[\"url\"]}!')
            return 0

    log('\n[X] Não foi possível sincronizar.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
