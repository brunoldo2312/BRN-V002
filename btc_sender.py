"""btc_sender.py — Envia BTC do cofre para endereços externos (OFFRAMP)
================================================================
v1.0 (2026-10-28):
  [NEW] Classe BTCSender. Constrói, assina e faz broadcast de tx BTC
        reais usando a WIF do cofre. Suporta mainnet e testnet.
  [NEW] Consulta UTXOs, saldo e taxa via mempool.space/blockstream.
  [NEW] Fallback entre 2 APIs de broadcast.
  [SECURITY] WIF nunca é logada. Tx é assinada em memória.
================================================================
"""
from __future__ import annotations

import os
import time
import logging
import threading

import requests

log = logging.getLogger("btc_sender")

# APIs públicas (fallback em ordem)
MEMPOOL_MAINNET  = "https://mempool.space/api"
MEMPOOL_TESTNET  = "https://mempool.space/testnet/api"
BLOCKSTREAM_MAIN = "https://blockstream.info/api"
BLOCKSTREAM_TEST = "https://blockstream.info/testnet/api"


class BTCSendError(Exception):
    pass


class BTCSender:
    def __init__(self, wif: str, network: str = "bitcoin"):
        if not wif or not wif.strip():
            raise BTCSendError("BTC_WIF vazio")

        self.wif = wif.strip()
        self.network = "testnet" if network == "testnet" else "bitcoin"
        self._lock = threading.RLock()

        # bitcoinlib usa "bitcoin" e "testnet" como identificadores
        from bitcoinlib.keys import Key
        try:
            self.key = Key(self.wif, network=self.network, compressed=True)
            self.address = self.key.address()
        except Exception as e:
            raise BTCSendError(f"WIF inválida para {network}: {e}")

        # APIs em ordem de tentativa
        if self.network == "testnet":
            self._api_candidates = [MEMPOOL_TESTNET, BLOCKSTREAM_TEST]
        else:
            self._api_candidates = [MEMPOOL_MAINNET, BLOCKSTREAM_MAIN]

    # ============================================================
    # CONSULTAS
    # ============================================================
    def get_address(self) -> str:
        return self.address

    def get_utxos(self) -> list:
        for base in self._api_candidates:
            try:
                r = requests.get(f"{base}/address/{self.address}/utxo", timeout=10)
                if r.status_code == 200:
                    return r.json() or []
            except Exception as e:
                log.debug(f"get_utxos {base}: {e}")
        return []

    def get_balance(self) -> int:
        return sum(int(u.get("value", 0)) for u in self.get_utxos())

    def get_fee_rate(self, priority: str = "halfHourFee") -> int:
        try:
            r = requests.get(f"{MEMPOOL_MAINNET}/v1/fees/recommended", timeout=5)
            if r.status_code == 200:
                data = r.json()
                return int(data.get(priority) or data.get("halfHourFee") or 10)
        except Exception:
            pass
        return 10  # fallback conservador (sat/vB)

    # ============================================================
    # ENVIO
    # ============================================================
    def send(self, dest: str, amount_sats: int,
             priority: str = "halfHourFee") -> dict:
        """
        Envia `amount_sats` do cofre para `dest`.

        Retorna dict com:
          {ok, txid, fee_sats, msg, from, to, amount_sats}
        """
        with self._lock:
            if amount_sats <= 0:
                return {"ok": False, "msg": "amount invalido"}

            # 1) Valida destino
            try:
                from bitcoinlib.keys import Address
                addr = Address(dest, network=self.network)
                dest_clean = addr.address
            except Exception as e:
                return {"ok": False, "msg": f"endereco BTC invalido: {e}"}

            if dest_clean == self.address:
                return {"ok": False, "msg": "destino == cofre"}

            # 2) Saldo
            balance = self.get_balance()
            if balance < amount_sats:
                return {"ok": False,
                        "msg": f"cofre insuficiente: {balance} < {amount_sats} sats"}

            # 3) Taxa
            fee_rate = self.get_fee_rate(priority)
            # bitcoinlib usa fee_per_kb; fee_rate é sat/vB → *1000 = sat/kB
            fee_per_kb = fee_rate * 1000

            # 4) Build + sign + broadcast via bitcoinlib
            try:
                tx = self.key.send(
                    [{"address": dest_clean, "value": amount_sats}],
                    fee=None,  # auto
                    fee_per_kb=fee_per_kb,
                    network=self.network,
                    broadcast=False,  # controlamos o broadcast
                )
            except Exception as e:
                return {"ok": False, "msg": f"build/sign falhou: {e}"}

            raw_hex = tx.raw_hex()
            local_txid = tx.txid
            fee_sats = int(getattr(tx, "fee", 0) or 0)

            # 5) Broadcast com fallback
            last_err = ""
            for base in self._api_candidates:
                try:
                    r = requests.post(f"{base}/tx", data=raw_hex,
                                      timeout=20,
                                      headers={"Content-Type": "text/plain"})
                    if r.status_code == 200 and len(r.text.strip()) == 64:
                        txid = r.text.strip()
                        log.info(f"[BTC SENDER] OK {amount_sats} sats -> "
                                 f"{dest_clean[:16]}… txid={txid[:16]}… fee={fee_sats}")
                        return {
                            "ok": True,
                            "txid": txid,
                            "fee_sats": fee_sats,
                            "from": self.address,
                            "to": dest_clean,
                            "amount_sats": amount_sats,
                        }
                    last_err = f"{base}: HTTP {r.status_code} {r.text[:120]}"
                    log.warning(f"[BTC SENDER] broadcast {last_err}")
                except Exception as e:
                    last_err = f"{base}: {e}"
                    log.warning(f"[BTC SENDER] broadcast {last_err}")

            return {"ok": False,
                    "msg": f"broadcast falhou em todas as APIs ({last_err})",
                    "txid_local": local_txid}
