"""app_wallet_v3.py — Ponte Python para a Carteira Gráfica BRN"""
import webview
import requests
import os

API_BASE = "http://127.0.0.1:5000"

class WalletApi:
    def __init__(self):
        pass

    def _get(self, path, **kwargs):
        try:
            return requests.get(f"{API_BASE}{path}", timeout=10, **kwargs).json()
        except Exception as e:
            return {"erro": str(e), "error": str(e)}

    def _post(self, path, **kwargs):
        try:
            return requests.post(f"{API_BASE}{path}", timeout=10, **kwargs).json()
        except Exception as e:
            return {"erro": str(e), "error": str(e)}

    def get_experiment_variant(self):
        return {"variant": "A"}

    def log_exposure(self, variant):
        return {"ok": True}

    def node_status(self):
        return self._get("/api/status")

    def sync_info(self):
        return self._get("/api/sync-info")

    def get_trust_nodes(self):
        return self._get("/api/trust/nodes")

    def peers_score(self):
        return self._get("/api/peers/score")

    def list_blocks(self, start, count):
        return self._get(f"/api/blocks?start={start}&limit={count}")

    def get_block(self, height):
        return {"block": self._get(f"/api/block/{height}")}

    def generate_wallet(self):
        return self._post("/api/nova-carteira")

    def portfolio(self, address):
        res = self._get(f"/api/portfolio/{address}")
        if "portfolio" in res:
            return res["portfolio"]
        return {"BRN": 0}

    def minhas_txs(self, address, limit):
        return self._get(f"/api/minhas-txs/{address}?limit={limit}")

    def transfer(self, frm, to, ticker, amount, privkey, pubkey):
        body = {"from": frm, "to": to, "amount": amount, "private_key": privkey}
        return self._post("/api/transfer", json=body)

    def verificar_recebimento(self, address, txid):
        return self._get(f"/api/verificar-recebimento/{address}/{txid}")

    def tx_status(self, txid):
        return self._get(f"/api/tx-status/{txid}")

    def bridge_claim(self, txid, address):
        body = {"btc_txid": txid, "buyer_brn": address}
        return self._post("/api/l2/claim", json=body)

    def save_wallet(self, name, password, address, priv, pub):
        from wallet import WalletManager
        return WalletManager.save_encrypted_wallet(name, password, address, priv, pub)

    def list_wallets(self):
        from wallet import WalletManager
        return WalletManager.list_wallets()

    def load_wallet(self, name, password):
        from wallet import WalletManager
        return WalletManager.load_encrypted_wallet(name, password)

    def get_active_wallet(self):
        from secure_store import load_current_wallet
        pw = os.environ.get("BRN_WALLET_SESSION_PASS", "")
        if pw:
            res = load_current_wallet(pw)
            if res.get("ok"):
                return res
        return {"ok": False}

    def mining_status(self):
        from miner_loop import get_miner
        m = get_miner(None)
        if not m:
            return {"running": False}
        return m.status()

    def start_mining(self, address, pubkey):
        from miner_loop import get_miner
        from server import _get_chain
        m = get_miner(_get_chain())
        ok, msg = m.start(address, pubkey)
        return {"ok": ok, "msg": msg}

    def stop_mining(self):
        from miner_loop import get_miner
        m = get_miner(None)
        if m:
            ok, msg = m.stop()
            return {"ok": ok, "msg": msg, "count": m.blocks_mined}
        return {"ok": False, "msg": "Miner nao iniciado"}

    def pool_status(self):
        from miner_pool_client import get_pool_client
        pc = get_pool_client()
        if pc:
            return pc.status()
        return {"running": False}

    def pool_start_mining(self, address, pubkey):
        from miner_pool_client import get_pool_client
        try:
            pc = get_pool_client(address, pubkey)
            if not pc:
                return {"ok": False, "msg": "Falha ao instanciar PoolClient"}
            ok, msg = pc.start_mining()
            return {"ok": ok, "msg": msg}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def pool_stop_mining(self):
        from miner_pool_client import stop_pool_client
        try:
            stop_pool_client()
            return {"ok": True, "msg": "Pool parado"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}


# Bloco para permitir testar a GUI diretamente (standalone)
if __name__ == "__main__":
    from pathlib import Path
    api = WalletApi()
    index_path = Path(__file__).parent / "index_wallet.html"
    
    if not index_path.exists():
        print("ERRO: index_wallet.html nao encontrado na mesma pasta!")
    else:
        print("Abrindo carteira grafica...")
        webview.create_window(
            "BRN RWA — Carteira Digital",
            url=index_path.resolve().as_uri(),
            js_api=api,
            width=1020, height=880,
            min_size=(820, 640),
            background_color="#0d1117",
        )
        webview.start(debug=False)
