#!/usr/bin/env python3
"""
sync_all_peers.py — Sincroniza de TODOS os peers, um a um (v2.0)

Novidades v2.0:
  - --only          : ignora auto-descoberta (Tailscale/env/json) e usa SÓ os --peer passados.
  - --peer          : especifica manualmente a URL ou IP:PORTA do peer.
  - --target-height : só sincroniza se a altura do peer for EXATAMENTE esta.

Exemplos:
  # Forçar sincronização APENAS do desktop-61ltr37 (altura 5026):
  python sync_all_peers.py --only --peer 100.70.107.15:5000 --target-height 5026

  # Testar sem baixar:
  python sync_all_peers.py --only --peer 100.70.107.15:5000 --dry-run

  # Modo antigo (auto-descoberta, maior altura):
  python sync_all_peers.py
"""
from __future__ import annotations
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print('[X] Falta: pip install requests')
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / 'brn_network.env'
DB_PATH = BASE_DIR / 'brn_v2_chain.db'


# ============================================================
# 1. Descoberta de peers
# ============================================================
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


def discover_tailscale() -> list[dict]:
    """Lista peers Tailscale via 'tailscale status --json'."""
    peers = []
    try:
        r = subprocess.run(
            ['tailscale', 'status', '--json'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        for p in (data.get('Peer') or {}).values():
            for ip in (p.get('TailscaleIPs') or []):
                if ip.startswith('100.'):
                    peers.append({
                        'name': p.get('HostName', '?'),
                        'ip': ip,
                        'os': p.get('OS', '?'),
                        'online': bool(p.get('Online')),
                        'source': 'tailscale',
                    })
                    break
    except Exception:
        pass
    return peers


def discover_from_env(env: dict) -> list[dict]:
    peers = []
    raw = env.get('BRN_BOOTSTRAP_PEERS', '')
    for item in raw.split(','):
        item = item.strip()
        if not item or ':' not in item:
            continue
        ip, _, port = item.partition(':')
        peers.append({
            'name': f'env:{ip}',
            'ip': ip,
            'os': '?',
            'online': True,
            'source': 'env',
            'p2p_port': int(port),
        })
    return peers


def discover_from_json_files() -> list[dict]:
    peers = []
    for fname in ('bootstrap_peers.json', 'peers_discovered.json', 'peers.json'):
        p = BASE_DIR / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, list):
                for item in data:
                    addr = str(item).strip() if not isinstance(item, dict) else item.get('address', '')
                    if addr and ':' in addr:
                        ip = addr.rsplit(':', 1)[0]
                        peers.append({
                            'name': f'{fname}:{ip}',
                            'ip': ip,
                            'os': '?',
                            'online': True,
                            'source': fname,
                        })
            elif isinstance(data, dict):
                for addr, _ in data.items():
                    if ':' in addr:
                        ip = addr.rsplit(':', 1)[0]
                        peers.append({
                            'name': f'{fname}:{ip}',
                            'ip': ip,
                            'os': '?',
                            'online': True,
                            'source': fname,
                        })
        except Exception:
            pass
    return peers


def parse_manual_peer(peer_str: str) -> dict:
    """Converte 'http://1.2.3.4:5000' ou '1.2.3.4:5000' em dict."""
    s = peer_str.strip().replace('http://', '').replace('https://', '')
    ip = s.rsplit(':', 1)[0] if ':' in s else s
    return {
        'name': f'manual:{ip}',
        'ip': ip,
        'os': '?',
        'online': True,
        'source': 'manual',
    }


def merge_peers(*lists) -> list[dict]:
    """Une listas por IP. Tailscale tem prioridade."""
    by_ip = {}
    priority = {'tailscale': 3, 'manual': 4, 'env': 2, 'bootstrap_peers.json': 1}
    for lst in lists:
        for p in lst:
            ip = p['ip']
            score = priority.get(p.get('source', ''), 0)
            if ip not in by_ip or score > priority.get(by_ip[ip].get('source', ''), 0):
                by_ip[ip] = p
    return list(by_ip.values())


# ============================================================
# 2. Teste de peers
# ============================================================
def tcp_check(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


def http_get_status(ip: str, port: int, timeout: float = 15.0) -> Optional[dict]:
    url = f'http://{ip}:{port}/api/status'
    try:
        r = requests.get(url, timeout=timeout, proxies={'http': None, 'https': None})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def probe_peer(peer: dict, http_port: int = 5000) -> dict:
    """Retorna peer com campos testados."""
    ip = peer['ip']
    result = dict(peer)
    result['http_port'] = http_port
    result['tcp_ok'] = False
    result['http_ok'] = False
    result['height'] = -1
    result['tip_hash'] = ''
    result['utxos'] = 0
    result['error'] = ''

    # 1. TCP
    if not tcp_check(ip, http_port):
        result['error'] = f'TCP {ip}:{http_port} fechado'
        return result
    result['tcp_ok'] = True

    # 2. HTTP /api/status
    status = http_get_status(ip, http_port)
    if not status:
        result['error'] = 'HTTP /api/status falhou'
        return result

    result['http_ok'] = True
    result['height'] = int(status.get('height', -1) or -1)
    result['tip_hash'] = status.get('tip_hash', '')
    result['utxos'] = int(status.get('utxos', 0) or 0)
    return result


# ============================================================
# 3. Download da blockchain
# ============================================================
def fetch_blocks(ip: str, port: int, start: int, batch: int = 100,
                 timeout: float = 60.0) -> Optional[list]:
    url = f'http://{ip}:{port}/api/blocks?start={start}&limit={batch}'
    try:
        r = requests.get(url, timeout=timeout,
                         proxies={'http': None, 'https': None})
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get('blocks', [])
    except Exception:
        return None


def download_from_peer(ip: str, port: int, remote_height: int,
                       local_height: int, batch: int = 100) -> list[dict]:
    """Baixa todos os blocos desde local_height+1 até remote_height."""
    all_blocks = []
    total = remote_height + 1
    t0 = time.time()

    for start in range(local_height + 1, remote_height + 1, batch):
        end = min(start + batch, remote_height + 1)
        actual_batch = end - start

        blocks = None
        for attempt in range(3):
            blocks = fetch_blocks(ip, port, start, actual_batch, timeout=60.0)
            if blocks is not None:
                break
            time.sleep(2)

        if not blocks:
            print(f'  [X] Lote {start}..{end-1} falhou após 3 tentativas')
            return all_blocks

        all_blocks.extend(blocks)
        elapsed = time.time() - t0
        downloaded = local_height + len(all_blocks) + 1
        pct = (downloaded / total) * 100
        rate = len(all_blocks) / max(0.1, elapsed)
        eta = (remote_height - downloaded) / max(0.1, rate) if rate > 0 else 0
        print(f'  [{downloaded}/{total}] {pct:.1f}% | {rate:.0f} bl/s | ETA {eta:.0f}s')

        if len(blocks) < actual_batch:
            break

    return all_blocks


def import_blocks(blocks: list[dict], db_path: str) -> tuple[int, int]:
    """Importa blocos no DB. Retorna (importados, altura_final)."""
    from db import ChainDB

    db = ChainDB(db_path)
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
            print(f'  [X] Erro no bloco #{h}: {e}')
            break

    after = db.height()
    db.close()
    return imported, after


# ============================================================
# 4. Orquestrador
# ============================================================
def main():
    ap = argparse.ArgumentParser(description='Sincroniza de todos os peers')
    ap.add_argument('--http-port', type=int, default=5000,
                    help='Porta HTTP dos peers (default: 5000)')
    ap.add_argument('--min-height', type=int, default=1,
                    help='Altura mínima para considerar o peer (default: 1)')
    ap.add_argument('--batch', type=int, default=100,
                    help='Blocos por requisição (default: 100)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Só testa peers, não baixa')
    
    # Novos argumentos v2.0
    ap.add_argument('--only', action='store_true',
                    help='Usa SÓ os --peer passados (ignora auto-descoberta)')
    ap.add_argument('--peer', action='append', default=[],
                    help='IP:PORTA ou URL do peer (ex: 100.70.107.15:5000)')
    ap.add_argument('--target-height', type=int, default=None,
                    help='Só sincroniza se a altura do peer for EXATAMENTE esta')
    args = ap.parse_args()

    print('=' * 70)
    print(' SYNC DE TODOS OS PEERS (v2.0)')
    if args.only:
        print(' MODO: --only (SÓ peers especificados manualmente)')
    if args.target_height is not None:
        print(f' FILTRO: altura EXATA = {args.target_height}')
    print('=' * 70)

    # 1. Descobrir peers
    if args.only:
        all_peers = [parse_manual_peer(p) for p in args.peer]
        if not all_peers:
            print('\n[X] --only passado, mas nenhum --peer especificado.')
            return 1
    else:
        env = load_env()
        ts_peers = discover_tailscale()
        env_peers = discover_from_env(env)
        json_peers = discover_from_json_files()
        manual_peers = [parse_manual_peer(p) for p in args.peer]
        
        all_peers = merge_peers(ts_peers, env_peers, json_peers, manual_peers)

    print(f'\n[1] Peers descobertos: {len(all_peers)}')
    for p in all_peers:
        print(f'  - {p["name"]:<35} {p["ip"]:<18} [{p["source"]}]')

    # 2. Testar cada peer
    print(f'\n[2] Testando {len(all_peers)} peers em HTTP:{args.http_port}...')
    probed = []
    for p in all_peers:
        print(f'  Testando {p["name"]} ({p["ip"]})...', end=' ', flush=True)
        result = probe_peer(p, args.http_port)
        probed.append(result)
        if result['http_ok']:
            print(f'OK — altura={result["height"]}, utxos={result["utxos"]}')
        elif result['tcp_ok']:
            print(f'TCP OK mas HTTP falhou')
        else:
            print(f'FALHOU ({result["error"]})')

    # 3. Filtrar peers válidos (HTTP ok + altura > local)
    local_height = -1
    try:
        from db import ChainDB
        db = ChainDB(DB_PATH)
        local_height = db.height()
        db.close()
    except Exception:
        pass

    valid = [p for p in probed if p['http_ok'] and p['height'] > local_height]

    # Aplicar filtro de altura exata, se fornecido
    if args.target_height is not None:
        valid = [p for p in valid if p['height'] == args.target_height]
        
    valid.sort(key=lambda p: p['height'], reverse=True)

    print(f'\n[3] Altura local: {local_height}')
    print(f'    Peers válidos: {len(valid)}')
    for p in valid:
        print(f'  - {p["name"]:<35} {p["ip"]:<18} altura={p["height"]}')

    if not valid:
        if args.target_height is not None:
            print(f'\n[X] Nenhum peer atende aos critérios (altura exata = {args.target_height} e altura > local).')
        else:
            print('\n[X] Nenhum peer tem blockchain mais alta que a local.')
        return 1

    if args.dry_run:
        print('\n[DRY-RUN] Sairia agora.')
        return 0

    # 4. Tentar cada peer (do mais alto para o mais baixo)
    print('\n[4] Tentando download...')
    for peer in valid:
        print(f'\n--- Tentando {peer["name"]} ({peer["ip"]}) altura={peer["height"]} ---')
        try:
            blocks = download_from_peer(
                peer['ip'], args.http_port,
                peer['height'], local_height, args.batch
            )
        except Exception as e:
            print(f'  [X] Erro: {e}')
            continue

        if not blocks:
            print(f'  [X] Nenhum bloco baixado')
            continue

        print(f'\n[*] Baixados {len(blocks)} blocos. Importando...')
        imported, final_h = import_blocks(blocks, str(DB_PATH))
        print(f'[OK] {imported} blocos importados. Altura final: {final_h}')

        if final_h >= peer['height']:
            print(f'\n[✓] Sincronizado com sucesso via {peer["ip"]}!')
            return 0

    print('\n[X] Não foi possível sincronizar de nenhum peer.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
