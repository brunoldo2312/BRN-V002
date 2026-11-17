# peer_discovery.py — Descoberta e Verificação de Peers via HTTP
# Versão: 1.0.0 | Data: 2026-11-17
# ✅ NÃO DEPENDE DE p2p_unified.py — Funciona 100% via HTTP/JSON
# ✅ Descoberta automática + verificação de altura + heartbeat

from __future__ import annotations
import json
import os
import socket
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

import logging

logger = logging.getLogger("peer-discovery")


class PeerHTTPClient:
    """Cliente leve para comunicação com peers via HTTP — sem P2P necessário"""
    
    DEFAULT_TIMEOUT = 5.0
    DEFAULT_PORTS = [5000, 8080, 3000]

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "BRN-Peer-Discovery/1.0",
            "Content-Type": "application/json"
        })

    def _normalize_url(self, address: str, port: int = None) -> str:
        """Garante que o endereço tem http:// e a porta correta"""
        if address.startswith(("http://", "https://")):
            parsed = urlparse(address)
            host = parsed.hostname or address.split("/")[0]
            port = parsed.port or port or 5000
        else:
            host = address.split(":")[0] if ":" in address else address
            port = port or (int(address.split(":")[1]) if ":" in address else 5000)
        return f"http://{host}:{port}"

    def ping(self, address: str, port: int = None) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Verifica se um peer está online e retorna seu status
        Retorna: (online, latencia_ms, dados)
        """
        url = self._normalize_url(address, port)
        
        try:
            start = time.perf_counter()
            r = self._session.get(f"{url}/health", timeout=self.timeout)
            latency = (time.perf_counter() - start) * 1000
            
            if r.status_code == 200:
                try:
                    dados = r.json()
                    return True, round(latency, 2), dados
                except json.JSONDecodeError:
                    return True, round(latency, 2), {"status": "ok"}
            return False, 0.0, {}
        except requests.exceptions.ConnectionError:
            return False, 0.0, {"error": "connection_failed"}
        except requests.exceptions.Timeout:
            return False, 0.0, {"error": "timeout"}
        except Exception as e:
            logger.debug("[PEER] Erro ao acessar %s: %s", url, e)
            return False, 0.0, {"error": str(e)}

    def get_status(self, address: str, port: int = None) -> Optional[Dict[str, Any]]:
        """Obtém altura, rede e estado do peer"""
        url = self._normalize_url(address, port)
        
        for rota in ["/api/status", "/status"]:
            try:
                r = self._session.get(rota, timeout=self.timeout)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                continue
        return None

    def get_peers_list(self, address: str, port: int = None) -> List[Dict[str, Any]]:
        """Pede a lista de peers conhecidos por outro nó — descoberta em cascata"""
        url = self._normalize_url(address, port)
        
        for rota in ["/api/peers", "/peers"]:
            try:
                r = self._session.get(rota, timeout=self.timeout)
                if r.status_code == 200:
                    dados = r.json()
                    if isinstance(dados, list):
                        return dados
                    if isinstance(dados, dict) and "peers" in dados:
                        return dados["peers"]
            except Exception:
                continue
        return []


class PeerDiscovery:
    """
    Sistema completo de descoberta e verificação de peers
    Funciona como substituto completo do Peer-Healer que usa P2P
    """

    def __init__(self, db, node_origin_url: str = None):
        self.db = db
        self.client = PeerHTTPClient()
        
        # Origem/Pool — sempre conhecido como ponto de partida
        self.origin_url = node_origin_url or os.environ.get("BRN_POOL_URL", "http://127.0.0.1:5000")
        self.origin_host = self._extract_host(self.origin_url)
        
        # Configuração de descoberta
        self.my_address = os.environ.get("BRN_NODE_URL", None)
        self.auto_scan_local = os.environ.get("BRN_SCAN_LOCAL_NETWORK", "1") == "1"
        
        # Estado interno
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._known_peers: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        
        # Intervalos (segundos)
        self.CHECK_INTERVAL = int(os.environ.get("BRN_PEER_CHECK_INTERVAL", "30"))
        self.DISCOVERY_INTERVAL = int(os.environ.get("BRN_DISCOVERY_INTERVAL", "120"))

    def _extract_host(self, url: str) -> str:
        """Extrai host:porta de uma URL"""
        parsed = urlparse(url)
        return f"{parsed.hostname}:{parsed.port or 5000}"

    def _is_self(self, address: str) -> bool:
        """Verifica se é o próprio nó — não precisa pingar"""
        if not self.my_address:
            # Sem endereço conhecido, verificar se é local
            local_ips = ["127.0.0.1", "localhost", socket.gethostbyname(socket.gethostname())]
            addr_host = address.split(":")[0] if ":" in address else address
            return addr_host in local_ips
        
        my_host = self._extract_host(self.my_address)
        return address == my_host

    # ============================================================
    # DESCOBERTA DE PEERS
    # ============================================================
    def discover_from_origin(self) -> List[str]:
        """
        Passo 1: Pergunta ao Origin quais peers ele conhece
        Igual ao que fazemos com o Origin, mas replicado para todos os nós
        """
        discovered = []
        
        logger.info("[DESCOBERTA] Consultando Origin: %s", self.origin_url)
        origin_peers = self.client.get_peers_list(self.origin_url)
        
        for peer_info in origin_peers:
            addr = peer_info.get("address") or peer_info.get("node_id")
            if not addr or self._is_self(addr):
                continue
            
            with self._lock:
                if addr not in self._known_peers:
                    logger.info("[DESCOBERTA] Novo peer via Origin: %s", addr)
                    discovered.append(addr)
                    self._known_peers[addr] = {
                        "address": addr,
                        "discovered_at": time.time(),
                        "source": "origin",
                        "last_checked": 0,
                        "online": False,
                        "height": 0
                    }
        
        return discovered

    def discover_from_known_peers(self):
        """
        Passo 2: Cascata — pergunta a cada peer conhecido quais peers ELES conhecem
        Assim a rede se autopropaga como uma teia
        """
        new_peers = []
        
        for addr in list(self._known_peers.keys()):
            if self._known_peers[addr].get("online") is not True:
                continue
            
            try:
                their_peers = self.client.get_peers_list(addr)
                for p in their_peers:
                    their_addr = p.get("address") or p.get("node_id")
                    if not their_addr or self._is_self(their_addr):
                        continue
                    
                    with self._lock:
                        if their_addr not in self._known_peers:
                            logger.info("[DESCOBERTA] Novo peer via %s: %s", addr, their_addr)
                            self._known_peers[their_addr] = {
                                "address": their_addr,
                                "discovered_at": time.time(),
                                "source": "peer-cascade",
                                "last_checked": 0,
                                "online": False,
                                "height": 0
                            }
                            new_peers.append(their_addr)
            except Exception as e:
                logger.debug("[DESCOBERTA] Não foi possível listar peers de %s: %s", addr, e)
        
        return new_peers

    def scan_local_network(self) -> List[str]:
        """
        Passo 3: Escaneia rede local em busca de outros nós
        Útil para nós na mesma rede LAN
        """
        if not self.auto_scan_local:
            return []
        
        discovered = []
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            rede_base = ".".join(local_ip.split(".")[:3])
            
            logger.debug("[DESCOBERTA] Escaneando rede: %s.x", rede_base)
            
            for i in range(2, 254):
                ip = f"{rede_base}.{i}"
                for porta in [5000, 8080]:
                    addr = f"{ip}:{porta}"
                    if self._is_self(addr):
                        continue
                    
                    online, _, _ = self.client.ping(ip, porta)
                    if online:
                        with self._lock:
                            if addr not in self._known_peers:
                                logger.info("[DESCOBERTA] Encontrado na rede local: %s", addr)
                                self._known_peers[addr] = {
                                    "address": addr,
                                    "discovered_at": time.time(),
                                    "source": "local-scan",
                                    "last_checked": 0,
                                    "online": True,
                                    "height": 0
                                }
                                discovered.append(addr)
        except Exception as e:
            logger.debug("[DESCOBERTA] Falha no escaneamento local: %s", e)
        
        return discovered

    # ============================================================
    # VERIFICAÇÃO DE PEERS
    # ============================================================
    def check_peer(self, address: str) -> Dict[str, Any]:
        """
        Verifica um peer individual — ping + status + altura
        Resultado é idêntico ao do P2P, mas via HTTP
        """
        online, latency, health_data = self.client.ping(address)
        
        resultado = {
            "address": address,
            "online": online,
            "latency_ms": latency,
            "last_checked": time.time(),
            "height": 0,
            "version": health_data.get("version", "") if health_data else "",
            "error": None
        }
        
        if online:
            status_data = self.client.get_status(address)
            if status_data:
                resultado["height"] = status_data.get("height", 0)
                resultado["network"] = status_data.get("network", "")
                resultado["peer_id"] = status_data.get("peer_id", "")
            
            # Atualizar no banco de dados (compatível com db.py)
            try:
                self.db.upsert_peer(
                    node_id=address,
                    address=address,
                    height=resultado["height"],
                    last_seen=time.time(),
                    version=resultado.get("version", "")
                )
            except Exception as e:
                logger.debug("[PEER] Não foi possível salvar no DB: %s", e)
        else:
            resultado["error"] = health_data.get("error", "offline") if health_data else "offline"
            logger.debug("[PEER] %s está offline (%s)", address, resultado["error"])
        
        # Atualizar cache local
        with self._lock:
            if address in self._known_peers:
                self._known_peers[address].update(resultado)
        
        return resultado

    def check_all_peers(self) -> Dict[str, Any]:
        """Verifica todos os peers conhecidos"""
        resultados = {
            "total": len(self._known_peers),
            "online": 0,
            "offline": 0,
            "peers": []
        }
        
        for addr in list(self._known_peers.keys()):
            r = self.check_peer(addr)
            resultados["peers"].append(r)
            if r["online"]:
                resultados["online"] += 1
            else:
                resultados["offline"] += 1
        
        logger.info(
            "[PEERS] Verificação completa: %d online / %d offline de %d total",
            resultados["online"], resultados["offline"], resultados["total"]
        )
        
        return resultados

    # ============================================================
    # LOOP PRINCIPAL — RODA EM BACKGROUND
    # ============================================================
    def _discovery_loop(self):
        """Loop de descoberta — roda em intervalo maior"""
        while self._running:
            try:
                # Descoberta em cascata
                self.discover_from_origin()
                self.discover_from_known_peers()
                self.scan_local_network()
            except Exception as e:
                logger.error("[DESCOBERTA] Erro no loop: %s", e)
            
            time.sleep(self.DISCOVERY_INTERVAL)

    def _check_loop(self):
        """Loop de verificação — roda mais frequentemente"""
        while self._running:
            try:
                self.check_all_peers()
            except Exception as e:
                logger.error("[PEERS] Erro na verificação: %s", e)
            
            time.sleep(self.CHECK_INTERVAL)

    def start(self):
        """Inicia os threads de descoberta e verificação"""
        if self._running:
            return
        
        self._running = True
        
        # Adicionar o Origin como peer conhecido inicial
        origin_host = self._extract_host(self.origin_url)
        if not self._is_self(origin_host):
            with self._lock:
                self._known_peers[origin_host] = {
                    "address
