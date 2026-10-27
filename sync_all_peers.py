#!/usr/bin/env python3
"""sync_all_peers.py — Sincroniza de TODOS os peers, um a um (v2.1)

================================================================
v2.1 (2026-10-27):
  [FIX CRÍTICO] import_blocks agora usa Blockchain.accept_block()
                em vez de db.add_block() + db.apply_tx() direto.
                Isso ativa TODA a validação de consenso:
                  - PoW (meets_difficulty)
                  - merkle root
                  - auth_tag HMAC
                  - coinbase <= reward + fees
                  - checkpoint (se presente)
                  - validators_sig (se trust injetado)
                  - assinaturas Ed25519 das txs
                Antes, blocos forjados/corrompidos de qualquer peer
                entravam no DB local sem verificação.
  [FIX] Falha em accept_block para o loop imediatamente (evita
        buraco na cadeia). Próximo sync continua do ponto correto.
  [NEW] --strict: injeta trust_network.json na chain de import
        para FORÇAR validação multi-sig.
  [NEW] --db PATH: sincroniza um DB alternativo (testes/restore).
  [NEW] Log claro de motivo de rejeição (msg do accept_block).
  [KEEP] Toda a lógica de descoberta (tailscale/env/json/manual)
         e merge_peers inalteradas.
  [KEEP] --only, --peer, --target-height, --dry-run, --batch,
         --http-port, --min-height.
v2.0:
  - --only, --peer, --target-height.
  - Auto-descoberta via tailscale/env/json.
v1.x:
  - Descoberta + download + import direto no DB (bypass).
================================================================

Exemplos:
  # Forçar sincronização APENAS do desktop-61ltr37 (altura 5026):
  python sync_all_peers.py --only --peer 100.70.107.15:5000 --target-height 5026

  # Testar sem baixar:
  python sync_all_peers.py --only --peer 100.70.107.15:5000 --dry-run

  # Sincronizar com validação multi-sig estrita:
  python sync_all_peers.py --strict

  # Sincronizar um DB alternativo:
  python sync_all_peers.py --db backups/chain_20261027_120000.db

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

__version__ = "2.1"

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / 'brn_network.env'
DB_PATH = BASE_DIR / 'brn_v2_chain.db'


def log(msg: str):
    print(msg, flush=True)


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

    if not tcp_check(ip, http_port):
        result['error'] = f'TCP {ip}:{http_port} fechado'
        return result
    result['tcp_ok'] = True

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
            log(f'  [X] Lote {start}..{end-1} falhou após 3 tentativas')
            return all_blocks

        all_blocks.extend(blocks)
        elapsed = time.time() - t0
        downloaded = local_height + len(all_blocks) + 1
        pct = (downloaded / total) * 100
        rate = len(all_blocks) / max(0.1, elapsed)
        eta = (remote_height - downloaded) / max(0.1, rate) if rate > 0 else 0
        log(f'  [{downloaded}/{total}] {pct:.1f}% | {rate:.0f} bl/s | ETA {eta:.0f}s')

        if len(blocks) < actual_batch:
            break

    return all_blocks


# ============================================================
# 4. [FIX v2.1] IMPORT VIA accept_block — valida consenso completo
# ============================================================
def import_blocks(blocks: list[dict], db_path: str,
                  strict_validators: bool = False) -> tuple[int, int]:
    """
    Importa blocos baixados usando Blockchain.accept_block().

    Comportamento:
      - Pula blocos que já existem no DB local (height ocupado).
      - Cada bloco é validado pelo consenso (PoW, merkle, auth_tag,
        coinbase-limit, checkpoint, validators_sig se strict).
      - Se um bloco é rejeitado, PARA. Não tenta "pular" — o próximo
        bloco dependeria do anterior, então a cadeia ficaria inválida.

    Retorna (importados, altura_final).
    """
    try:
        from blockchain import Blockchain
    except ImportError as e:
        log(f'  [X] blockchain.py indisponível: {e}')
        return (0, -1)

    bc = Blockchain(str(db_path), auto_genesis=False)

    # [NEW v2.1] --strict: injeta trust para forçar multi-sig
    if strict_validators:
        try:
            from trust_network import TrustNetwork
            trust_path = BASE_DIR / 'trust_network.json'
            if trust_path.exists():
                tn = TrustNetwork(str(trust_path))
                bc.set_trust_net(tn)
                n_v = len(bc._get_active_validators()) if hasattr(
                    bc, '_get_active_validators') else 0
                log(f'  [strict] trust injetado ({n_v} validador(es) ativo(s))')
            else:
                log('  [strict] trust_network.json não existe — modo bootstrap')
        except Exception as e:
            log(f'  [!] --strict falhou ao injetar trust: {e} (seguindo bootstrap)')

    before = bc.db.height()
    imported = 0

    try:
        for block in blocks:
            h = block.get('height')
            if h is None:
                log('  [!] bloco sem campo height — ignorando')
                continue
            try:
                if bc.db.get_block(h) is not None:
                    continue
            except Exception:
                pass

            ok, msg = bc.accept_block(block)
            if not ok:
                log(f'  [X] bloco #{h} rejeitado: {msg}')
                break
            imported += 1
    finally:
        after = bc.db.height()
        try:
            bc.db.close()
        except Exception:
            pass

    return imported, after


# ============================================================
# 5. Orquestrador
# ============================================================
def main():
    ap = argparse.ArgumentParser(
        description='Sincroniza de todos os peers usando consenso (accept_block).'
    )
    ap.add_argument('--http-port', type=int, default=5000,
                    help='Porta HTTP dos peers (default: 5000)')
    ap.add_argument('--min-height', type=int, default=1,
                    help='Altura mínima para considerar o peer (default: 1)')
    ap.add_argument('--batch', type=int, default=100,
                    help='Blocos por requisição (default: 100)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Só testa peers, não baixa')

    ap.add_argument('--only', action='store_true',
                    help='Usa SÓ os --peer passados (ignora auto-descoberta)')
    ap.add_argument('--peer', action='append', default=[],
                    help='IP:PORTA ou URL do peer (ex: 100.70.107.15:5000)')
    ap.add_argument('--target-height', type=int, default=None,
                    help='Só sincroniza se a altura do peer for EXATAMENTE esta')
    ap.add_argument('--strict', action='store_true',
                    help='Força validação multi-sig (injeta trust_network.json)')
    ap.add_argument('--db', type=str, default=None,
                    help='DB alternativo (default: brn_v2_chain.db)')
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH

    log('=' * 70)
    log(f' SYNC DE TODOS OS PEERS (v{__version__})')
    log(' MODO: consenso ativo (accept_block)')
    if args.strict:
        log(' MODO: --strict (multi-sig obrigatório)')
    if args.only:
        log(' MODO: --only (SÓ peers especificados manualmente)')
    if args.target_height is not None:
        log(f' FILTRO: altura EXATA = {args.target_height}')
    if args.db:
        log(f' DB: {db_path}')
    log('=' * 70)

    # 1. Descobrir peers
    if args.only:
        all_peers = [parse_manual_peer(p) for p in args.peer]
        if not all_peers:
            log('\n[X] --only passado, mas nenhum --peer especificado.')
            return 1
    else:
        env = load_env()
        ts_peers = discover_tailscale()
        env_peers = discover_from_env(env)
        json_peers = discover_from_json_files()
        manual_peers = [parse_manual_peer(p) for p in args.peer]

        all_peers = merge_peers(ts_peers, env_peers, json_peers, manual_peers)

    log(f'\n[1] Peers descobertos: {len(all_peers)}')
    for p in all_peers:
        log(f'  - {p["name"]:<35} {p["ip"]:<18} [{p["source"]}]')

    # 2. Testar cada peer
    log(f'\n[2] Testando {len(all_peers)} peers em HTTP:{args.http_port}...')
    probed = []
    for p in all_peers:
        log(f'  Testando {p["name"]} ({p["ip"]})...')
        result = probe_peer(p, args.http_port)
        probed.append(result)
        if result['http_ok']:
            log(f'    OK — altura={result["height"]}, utxos={result["utxos"]}')
        elif result['tcp_ok']:
            log(f'    TCP OK mas HTTP falhou')
        else:
            log(f'    FALHOU ({result["error"]})')

    # 3. Filtrar peers válidos (HTTP ok + altura > local)
    try:
        from db import ChainDB
        db = ChainDB(str(db_path))
        local_height = db.height()
        db.close()
    except Exception as e:
        log(f'[!] Falha lendo DB local: {e}')
        local_height = -1

    valid = [p for p in probed if p['http_ok'] and p['height'] > local_height]

    if args.target_height is not None:
        valid = [p for p in valid if p['height'] == args.target_height]

    valid.sort(key=lambda p: p['height'], reverse=True)

    log(f'\n[3] Altura local: {local_height}')
    log(f'    Peers válidos: {len(valid)}')
    for p in valid:
        log(f'  - {p["name"]:<35} {p["ip"]:<18} altura={p["height"]}')

    if not valid:
        if args.target_height is not None:
            log(f'\n[X] Nenhum peer atende aos critérios '
                f'(altura exata = {args.target_height} e altura > local).')
        else:
            log('\n[X] Nenhum peer tem blockchain mais alta que a local.')
        return 1

    if args.dry_run:
        log('\n[DRY-RUN] Sairia agora.')
        return 0

    # 4. Tentar cada peer (do mais alto para o mais baixo)
    log('\n[4] Baixando e validando...')
    for peer in valid:
        log(f'\n--- Tentando {peer["name"]} ({peer["ip"]}) altura={peer["height"]} ---')
        try:
            blocks = download_from_peer(
                peer['ip'], args.http_port,
                peer['height'], local_height, args.batch
            )
        except Exception as e:
            log(f'  [X] Erro: {e}')
            continue

        if not blocks:
            log(f'  [X] Nenhum bloco baixado')
            continue

        log(f'\n[*] Baixados {len(blocks)} blocos. Importando via accept_block...')
        imported, final_h = import_blocks(
            blocks, str(db_path), strict_validators=args.strict
        )
        log(f'[RESULTADO] {imported} importado(s). Altura final: {final_h}')

        if final_h >= peer['height']:
            log(f'\n[✓] Sincronizado com sucesso via {peer["ip"]}!')
            return 0

        if imported == 0:
            log('  [!] Nenhum bloco importado — tentando próximo peer')
            continue

        if final_h < peer['height']:
            log(f'  [!] Parcial ({final_h}/{peer["height"]}) — '
                f'rejeição em algum bloco. Tentando próximo peer.')

    log('\n[X] Não foi possível sincronizar de nenhum peer.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
