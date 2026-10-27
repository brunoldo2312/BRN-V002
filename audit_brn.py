"""audit_brn.py - Auditoria completa e SEGURA do no BRN (READ-ONLY)

Nunca envia segredos pela rede. Nunca modifica arquivos.
Fingerprints SHA-256 sao irreversiveis.

Uso:
    python audit_brn.py              # auditoria completa (local + peers)
    python audit_brn.py --local-only # so local (sem rede)
    python audit_brn.py --card       # cartao compacto (SEGURO compartilhar)
    python audit_brn.py --json       # saida JSON
"""
import argparse
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path

try:
    import requests
    _HAS_REQ = True
except ImportError:
    _HAS_REQ = False

BASE = Path(__file__).resolve().parent

# ============================================================
# HELPERS
# ============================================================
def _fp(value: str) -> str:
    if not value:
        return "-----"
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:16]

def _mask(value: str) -> str:
    if not value:
        return "(vazio)"
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]

def _read_env(path: Path) -> dict:
    result = {}
    if not path.exists():
        return result
    try:
        raw_bytes = path.read_bytes()
        bom = raw_bytes[:3] == b"\xef\xbb\xbf"
        text = path.read_text(encoding="utf-8-sig")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            result[k.strip()] = v
        result["__BOM__"] = "SIM" if bom else "nao"
    except Exception as e:
        result["__ERRO__"] = str(e)
    return result

def _file_version(path: Path, needle: str = "__version__") -> str:
    if not path.exists():
        return "MISSING"
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")[:3000]
        for line in content.splitlines():
            if needle in line and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return "ok"
    except Exception:
        return "ok"

def _tcp_test(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False

def _http_get(url: str, timeout: float = 3.0):
    if not _HAS_REQ:
        return None
    try:
        r = requests.get(url, timeout=timeout,
                         proxies={"http": None, "https": None},
                         headers={"User-Agent": "brn-audit/1.0"})
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

# ============================================================
# AUDITORIA LOCAL
# ============================================================
def audit_local():
    out = {
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "cwd": str(BASE),
        "timestamp": time.time(),
    }

    # 1. .env
    env_path = BASE / "brn_network.env"
    env = _read_env(env_path)
    env_path_exists = env_path.exists()
    out["env"] = {
        "exists": env_path_exists,
        "bom": env.get("__BOM__", "nao"),
        "keys_count": len([k for k in env if not k.startswith("__")]),
        "error": env.get("__ERRO__", ""),
    }

    # 2. Segredos criticos (fingerprint apenas)
    secrets = ["BRN_NETWORK_SECRET", "BRN_NODE_PASSWORD",
               "BRN_WALLET_SESSION_PASS", "BRN_ADMIN_TOKEN",
               "BRN_WEB_USER", "BRN_WEB_PASS"]
    out["secrets"] = {}
    for k in secrets:
        v = env.get(k, "")
        out["secrets"][k] = {
            "present": bool(v),
            "len": len(v),
            "fingerprint": _fp(v) if v else "-----",
            "masked": _mask(v) if v else "",
        }

    # 3. Config publica (nao sensivel)
    safe_keys = ["BRN_CHAIN_ID", "BRN_P2P_PORT", "BRN_WEB_PORT",
                 "BRN_WEB_HOST", "BRN_IS_ORIGIN", "BRN_HTTPS",
                 "BRN_TRACKER", "BRN_BOOTSTRAP_PEERS", "BRN_READ_ONLY"]
    out["config"] = {k: env.get(k, "") for k in safe_keys}

    # 4. Arquivos essenciais
    essential = [
        ("blockchain.py", "v"),
        ("db.py", "v"),
        ("crypto.py", "v"),
        ("wallet.py", "v"),
        ("bech32.py", "v"),
        ("secure_store.py", "v"),
        ("sync_manager.py", "v"),
        ("chain_validator.py", "v"),
        ("checkpoints.py", "v"),
        ("p2p_unified.py", "v"),
        ("p2p_auth.py", "v"),
        ("p2p_cipher.py", "v"),
        ("p2p_secure.py", "v"),
        ("discovery_v2.py", "v"),
        ("fork_guard.py", "v"),
        ("miner_loop.py", "v"),
        ("mining_pool.py", "v"),
        ("miner_pool_client.py", "v"),
        ("server.py", "v"),
        ("explorer.py", "v"),
        ("reorg.py", "v"),
        ("l2_manager.py", "v"),
        ("btc_watcher.py", "v"),
        ("trust_network.py", "v"),
        ("main.py", "v"),
        ("version.py", ""),
    ]
    out["files"] = {}
    missing = []
    for fname, _ in essential:
        p = BASE / fname
        if not p.exists():
            out["files"][fname] = "MISSING"
            missing.append(fname)
        else:
            ver = _file_version(p) if fname.endswith(".py") else "ok"
            out["files"][fname] = ver
    out["missing_files"] = missing

    # 5. Arquivos de estado
    state_files = ["brn_v2_chain.db", "bootstrap_peers.json",
                   "checkpoints.json", "trust_network.json",
                   "current_wallet.enc", "node_identity.enc",
                   "peers_discovered.json", "peers.json"]
    out["state_files"] = {}
    for fname in state_files:
        p = BASE / fname
        if p.exists():
            try:
                size = p.stat().st_size
                out["state_files"][fname] = f"{size} bytes"
            except Exception:
                out["state_files"][fname] = "presente"
        else:
            out["state_files"][fname] = "ausente"

    # 6. DB
    try:
        from db import ChainDB
        db = ChainDB(str(BASE / "brn_v2_chain.db"))
        height = db.height()
        tip = db.tip_hash()
        genesis_hash = "?"
        try:
            g = db.get_block(0)
            if g:
                genesis_hash = g.get("hash", "?")
        except Exception:
            pass
        out["db"] = {
            "height": height,
            "tip": tip,
            "genesis": genesis_hash,
        }
        try:
            db.close()
        except Exception:
            pass
    except Exception as e:
        out["db"] = {"error": str(e)}

    # 7. Wallet (tenta decifrar com BRN_WALLET_SESSION_PASS)
    try:
        if env.get("BRN_WALLET_SESSION_PASS"):
            from secure_store import load_current_wallet
            w = load_current_wallet(env["BRN_WALLET_SESSION_PASS"])
            if w and w.get("ok"):
                out["wallet"] = {
                    "decrypted": True,
                    "address": w.get("address", ""),
                    "pubkey_fp": _fp(w.get("public_key") or w.get("pub_hex") or ""),
                }
            else:
                out["wallet"] = {"decrypted": False,
                                 "msg": (w or {}).get("msg", "?")}
        else:
            out["wallet"] = {"decrypted": False, "msg": "sem senha"}
    except Exception as e:
        out["wallet"] = {"decrypted": False, "msg": str(e)}

    # 8. Bootstrap peers
    try:
        bp = BASE / "bootstrap_peers.json"
        if bp.exists():
            out["bootstrap_peers"] = json.loads(bp.read_text(encoding="utf-8"))
        else:
            out["bootstrap_peers"] = []
    except Exception:
        out["bootstrap_peers"] = []

    return out

# ============================================================
# PROBE PEERS
# ============================================================
def probe_peers(peers_list, local):
    results = []
    local_genesis = local.get("db", {}).get("genesis", "?")
    local_height = local.get("db", {}).get("height", -1)

    for peer in peers_list:
        if not isinstance(peer, str) or ":" not in peer:
            continue
        ip, port_s = peer.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            continue

        # Tenta portas HTTP (5000 padrao)
        http_port = 5000

        r = {
            "peer": peer,
            "ip": ip,
            "p2p_port": port,
            "http_port": http_port,
            "tcp_p2p": _tcp_test(ip, port),
            "tcp_http": _tcp_test(ip, http_port),
            "chain_info": None,
            "health": None,
            "error": "",
        }

        if r["tcp_http"]:
            ci = _http_get(f"http://{ip}:{http_port}/api/chain-info")
            if ci:
                r["chain_info"] = {
                    "height": ci.get("height", -1),
                    "tip_hash": ci.get("tip_hash", ""),
                    "difficulty": ci.get("difficulty", 0),
                }
            h = _http_get(f"http://{ip}:{http_port}/health")
            if h:
                r["health"] = {
                    "chain_injected": h.get("chain_injected"),
                    "p2p_injected": h.get("p2p_injected"),
                    "sync_injected": h.get("sync_injected"),
                    "pool_injected": h.get("pool_injected"),
                }
            # Genesis via /api/blocks?start=0
            if r["chain_info"] and r["chain_info"]["height"] >= 0:
                blks = _http_get(f"http://{ip}:{http_port}/api/blocks?start=0&limit=1")
                if blks and blks.get("blocks"):
                    r["genesis"] = blks["blocks"][0].get("hash", "?")

        # Compara com local
        r["same_genesis"] = None
        if r.get("genesis") and local_genesis != "?":
            r["same_genesis"] = (r["genesis"] == local_genesis)
        r["behind"] = None
        if r.get("chain_info") and r["chain_info"]["height"] >= 0:
            r["behind"] = local_height - r["chain_info"]["height"]

        results.append(r)

    return results

# ============================================================
# RENDER
# ============================================================
def render_full(local, peers):
    print("=" * 70)
    print(f" BRN AUDIT — {local['hostname']} ({local['platform']})")
    print(f" cwd: {local['cwd']}")
    print(f" data: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(local['timestamp']))}")
    print("=" * 70)

    # [1] .env
    e = local["env"]
    print(f"\n[1] ARQUIVO .env")
    print(f"  existe            : {'SIM' if e['exists'] else 'NAO'}")
    print(f"  BOM presente      : {e['bom']}" + ("  <-- PROBLEMA!" if e['bom'] == "SIM" else ""))
    print(f"  chaves            : {e['keys_count']}")
    if e.get("error"):
        print(f"  erro              : {e['error']}")

    # [2] Segredos
    print(f"\n[2] SEGREDOS (fingerprint apenas, NUNCA compartilhe o .env)")
    for k, info in local["secrets"].items():
        if info["present"]:
            print(f"  {k:28s}: PRESENT  len={info['len']:3d}  fp={info['fingerprint']}")
        else:
            print(f"  {k:28s}: AUSENTE")

    # [3] Config
    print(f"\n[3] CONFIG")
    for k, v in local["config"].items():
        if v:
            print(f"  {k:28s}: {v}")

    # [4] Arquivos
    print(f"\n[4] ARQUIVOS ESSENCIAIS")
    for fname, ver in local["files"].items():
        if ver == "MISSING":
            print(f"  {fname:30s}: ❌ FALTANDO")
        else:
            print(f"  {fname:30s}: ✅ {ver}")

    # [5] Estado
    print(f"\n[5] ARQUIVOS DE ESTADO")
    for fname, status in local["state_files"].items():
        print(f"  {fname:30s}: {status}")

    # [6] DB
    db = local["db"]
    print(f"\n[6] BLOCKCHAIN LOCAL")
    if "error" in db:
        print(f"  ERRO: {db['error']}")
    else:
        print(f"  height            : {db['height']}")
        print(f"  tip_hash          : {db['tip'][:48]}...")
        print(f"  genesis_hash      : {db['genesis'][:48]}...")

    # [7] Wallet
    w = local.get("wallet", {})
    print(f"\n[7] WALLET")
    if w.get("decrypted"):
        print(f"  decifrada         : SIM")
        print(f"  address           : {w.get('address', '?')}")
        print(f"  pubkey_fp         : {w.get('pubkey_fp', '?')}")
    else:
        print(f"  decifrada         : NAO ({w.get('msg', '?')})")

    # [8] Bootstrap
    print(f"\n[8] BOOTSTRAP PEERS")
    bp = local.get("bootstrap_peers", [])
    if bp:
        for p in bp:
            print(f"  - {p}")
    else:
        print(f"  (vazio)")

    # [9] Peers HTTP
    if peers:
        print(f"\n[9] PROBE PEERS (HTTP :5000)")
        print(f"  {'PEER':<22} {'TCP-P2P':<8} {'TCP-HTTP':<10} {'HEIGHT':<8} {'DIFF':<5} {'SAME-GENESIS':<15} BEHIND")
        print(f"  {'-'*22} {'-'*8} {'-'*10} {'-'*8} {'-'*5} {'-'*15} {'-'*8}")
        for p in peers:
            ci = p.get("chain_info") or {}
            gen = "?" if p.get("same_genesis") is None else ("SIM" if p["same_genesis"] else "NAO (FORK!)")
            behind = "?" if p.get("behind") is None else str(p["behind"])
            print(f"  {p['peer']:<22} {str(p['tcp_p2p']):<8} {str(p['tcp_http']):<10} "
                  f"{str(ci.get('height', '?')):<8} {str(ci.get('difficulty', '?')):<5} "
                  f"{gen:<15} {behind}")

        # Alertas
        print(f"\n[10] ALERTAS DE REDE")
        alerts = 0
        for p in peers:
            if p.get("same_genesis") is False:
                print(f"  🚨 FORK: {p['peer']} tem genesis diferente!")
                alerts += 1
            if p.get("chain_info") and p["chain_info"].get("difficulty", 0) < 3:
                print(f"  ⚠️  {p['peer']} com difficulty baixa ({p['chain_info']['difficulty']}) — possivel BRN_FORCE_DIFFICULTY")
                alerts += 1
        if alerts == 0:
            print(f"  ✅ Nenhum problema detectado entre peers")

    print("\n" + "=" * 70)
    print(" Para comparar segredos entre maquinas, rode --card em CADA uma")
    print(" e compare o campo 'network_secret_fp' (deve ser IDENTICO)")
    print("=" * 70)

def render_card(local, peers):
    """Cartao compacto SEGURO para compartilhar entre maquinas."""
    db = local.get("db", {})
    s = local.get("secrets", {})
    print("BRN-AUDIT-CARD-v1")
    print(f"machine={local['hostname']}")
    print(f"platform={local['platform']}")
    print(f"chain_id={local.get('config', {}).get('BRN_CHAIN_ID', '')}")
    print(f"is_origin={local.get('config', {}).get('BRN_IS_ORIGIN', '')}")
    print(f"p2p_port={local.get('config', {}).get('BRN_P2P_PORT', '')}")
    print(f"network_secret_fp={s.get('BRN_NETWORK_SECRET', {}).get('fingerprint', '-----')}")
    print(f"node_password_fp={s.get('BRN_NODE_PASSWORD', {}).get('fingerprint', '-----')}")
    print(f"wallet_pass_fp={s.get('BRN_WALLET_SESSION_PASS', {}).get('fingerprint', '-----')}")
    print(f"genesis={db.get('genesis', '?')}")
    print(f"height={db.get('height', '?')}")
    print(f"tip={db.get('tip', '?')}")
    print(f"wallet_address={local.get('wallet', {}).get('address', '(nao decifrada)')}")
    print(f"missing_files={','.join(local.get('missing_files', []))}")
    print(f"env_bom={local.get('env', {}).get('bom', '?')}")
    if peers:
        print(f"peers_seen={len(peers)}")
        for p in peers:
            gen = "?" if p.get("same_genesis") is None else ("SIM" if p["same_genesis"] else "FORK")
            h = (p.get("chain_info") or {}).get("height", "?")
            print(f"peer={p['peer']} height={h} genesis_match={gen}")

# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="BRN audit - READ-ONLY, sem vazamento")
    ap.add_argument("--local-only", action="store_true", help="nao testa peers")
    ap.add_argument("--card", action="store_true", help="cartao compacto para compartilhar")
    ap.add_argument("--json", action="store_true", help="saida JSON")
    ap.add_argument("--peer", action="append", default=[], help="adiciona peer IP:PORT manualmente")
    args = ap.parse_args()

    print("[audit] Lendo arquivos locais...", file=sys.stderr)
    local = audit_local()

    peers_probe = []
    if not args.local_only:
        # Combina bootstrap_peers + --peer
        peers_list = list(local.get("bootstrap_peers", []))
        for p in args.peer:
            if p not in peers_list:
                peers_list.append(p)

        if peers_list:
            print(f"[audit] Testando {len(peers_list)} peer(s)...", file=sys.stderr)
            peers_probe = probe_peers(peers_list, local)

    if args.json:
        print(json.dumps({"local": local, "peers": peers_probe}, indent=2, default=str))
    elif args.card:
        render_card(local, peers_probe)
    else:
        render_full(local, peers_probe)

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
