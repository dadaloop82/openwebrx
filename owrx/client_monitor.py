"""
Client Connection Monitor for OpenWebRX
Monitors active WebSocket connections and distinguishes local from remote clients
Used to enable auto-mode when no remote clients are connected
"""

import os
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Set, Optional, Callable
from ipaddress import ip_address, ip_network
import json

logger = logging.getLogger(__name__)


class ClientInfo:
    """Information about a connected client"""
    
    def __init__(self, client_id: str, ip: str, user_agent: str = ""):
        self.client_id = client_id
        self.ip = ip
        self.user_agent = user_agent
        self.connected_at = datetime.now()
        self.last_seen = datetime.now()
    
    def update_activity(self):
        """Update last seen timestamp"""
        self.last_seen = datetime.now()
    
    def duration_seconds(self) -> int:
        """Get connection duration in seconds"""
        return int((datetime.now() - self.connected_at).total_seconds())
    
    def to_dict(self) -> Dict:
        return {
            'client_id': self.client_id,
            'ip': self.ip,
            'user_agent': self.user_agent,
            'connected_at': self.connected_at.isoformat(),
            'duration_seconds': self.duration_seconds()
        }


class ClientMonitor:
    """Monitors client connections and determines if auto-mode should be active"""
    
    instance = None
    lock = threading.Lock()
    
    @staticmethod
    def get_instance():
        with ClientMonitor.lock:
            if ClientMonitor.instance is None:
                ClientMonitor.instance = ClientMonitor()
        return ClientMonitor.instance
    
    def __init__(self):
        self.clients: Dict[str, ClientInfo] = {}
        self.clients_lock = threading.Lock()
        self.config = self._load_config()
        self.callbacks = {
            'remote_client_connected': [],
            'remote_client_disconnected': [],
            'all_remote_clients_gone': []
        }
        self.monitor_thread = None
        self.running = False
        
        logger.info("ClientMonitor initialized")
    
    def _load_config(self) -> Dict:
        """Load configuration"""
        default_config = {
            'enabled': True,
            'consider_local_clients': False,
            'local_ip_whitelist': [
                '127.0.0.1',
                '::1',
                '192.168.0.0/16',
                '10.0.0.0/8',
                '172.16.0.0/12'
            ],
            'check_interval_seconds': 5
        }
        
        # Try to load from config file
        try:
            from owrx.config.core import CoreConfig
            config_file = os.path.join(
                CoreConfig().get_data_directory(),
                'auto_mode_config.json'
            )
            
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    return data.get('client_monitor', default_config)
        except Exception as e:
            logger.debug("Using default client monitor config: %s", e)
        
        return default_config
    
    def is_local_ip(self, ip_str: str) -> bool:
        """Check if IP is considered local"""
        try:
            ip = ip_address(ip_str)
            
            # Check against whitelist
            for network_str in self.config['local_ip_whitelist']:
                try:
                    if '/' in network_str:
                        network = ip_network(network_str, strict=False)
                        if ip in network:
                            return True
                    else:
                        if ip == ip_address(network_str):
                            return True
                except:
                    continue
            
            return False
        except:
            return False
    
    def register_callback(self, event: str, callback: Callable):
        """Register a callback for an event"""
        if event in self.callbacks:
            self.callbacks[event].append(callback)
            logger.debug("Registered callback for event: %s", event)
    
    def _trigger_callbacks(self, event: str, *args, **kwargs):
        """Trigger all callbacks for an event"""
        for callback in self.callbacks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                logger.error("Error in callback for %s: %s", event, e)
    
    def start(self):
        """Start monitoring"""
        if not self.config['enabled']:
            logger.info("ClientMonitor disabled in config")
            return
        
        if self.running:
            logger.warning("ClientMonitor already running")
            return
        
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("👁️  CLIENT MONITOR STARTED")
        logger.info("   Consider local clients: %s", self.config['consider_local_clients'])
        logger.info("   Local networks: %s", ', '.join(self.config['local_ip_whitelist']))
        logger.info("═══════════════════════════════════════════════════")
    
    def stop(self):
        """Stop monitoring"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("ClientMonitor stopped")
    
    def client_connected(self, client_id: str, ip: str, user_agent: str = ""):
        """Register a new client connection"""
        with self.clients_lock:
            client = ClientInfo(client_id, ip, user_agent)
            self.clients[client_id] = client
            
            is_local = self.is_local_ip(ip)
            logger.info("Client connected: %s from %s (%s)", 
                       client_id, ip, "LOCAL" if is_local else "REMOTE")
            
            # Trigger callback if it's a remote client
            if not is_local:
                self._trigger_callbacks('remote_client_connected', client)
    
    def client_disconnected(self, client_id: str):
        """Register a client disconnection"""
        with self.clients_lock:
            if client_id in self.clients:
                client = self.clients[client_id]
                is_local = self.is_local_ip(client.ip)
                
                logger.info("Client disconnected: %s from %s (duration: %ds)", 
                           client_id, client.ip, client.duration_seconds())
                
                del self.clients[client_id]
                
                # Check if this was the last remote client
                if not is_local:
                    self._trigger_callbacks('remote_client_disconnected', client)
                    
                    if not self.has_remote_clients():
                        logger.info("🎯 All remote clients gone - AUTO MODE can activate")
                        self._trigger_callbacks('all_remote_clients_gone')
    
    def client_activity(self, client_id: str):
        """Update client activity timestamp"""
        with self.clients_lock:
            if client_id in self.clients:
                self.clients[client_id].update_activity()
    
    def has_remote_clients(self) -> bool:
        """Check if any remote clients are connected"""
        with self.clients_lock:
            if self.config['consider_local_clients']:
                # All clients count
                return len(self.clients) > 0
            else:
                # Only remote clients count
                for client in self.clients.values():
                    if not self.is_local_ip(client.ip):
                        return True
                return False
    
    def get_client_count(self) -> Dict[str, int]:
        """Get count of local and remote clients"""
        with self.clients_lock:
            local = 0
            remote = 0
            
            for client in self.clients.values():
                if self.is_local_ip(client.ip):
                    local += 1
                else:
                    remote += 1
            
            return {
                'total': len(self.clients),
                'local': local,
                'remote': remote
            }
    
    def get_clients_info(self) -> list:
        """Get information about all connected clients"""
        with self.clients_lock:
            return [
                {
                    **client.to_dict(),
                    'is_local': self.is_local_ip(client.ip)
                }
                for client in self.clients.values()
            ]
    
    def _monitor_loop(self):
        """Background monitoring loop"""
        last_remote_count = 0
        
        while self.running:
            try:
                counts = self.get_client_count()
                current_remote_count = counts['remote']
                
                # Log status every minute
                if int(time.time()) % 60 == 0:
                    logger.debug("Client status: %d total (%d local, %d remote)", 
                               counts['total'], counts['local'], counts['remote'])
                
                # Detect transition to no remote clients
                if last_remote_count > 0 and current_remote_count == 0:
                    if not self.config['consider_local_clients'] or counts['total'] == 0:
                        logger.info("🎯 Transition: Remote clients -> None (AUTO MODE ready)")
                        self._trigger_callbacks('all_remote_clients_gone')
                
                last_remote_count = current_remote_count
                
                time.sleep(self.config['check_interval_seconds'])
                
            except Exception as e:
                logger.error("Error in monitor loop: %s", e)
                time.sleep(5)
    
    def get_status(self) -> Dict:
        """Get current monitor status"""
        counts = self.get_client_count()
        return {
            'enabled': self.config['enabled'],
            'running': self.running,
            'has_remote_clients': self.has_remote_clients(),
            'auto_mode_allowed': not self.has_remote_clients(),
            'clients': counts,
            'consider_local_clients': self.config['consider_local_clients']
        }


def init_client_monitor():
    """Initialize and start the client monitor"""
    try:
        monitor = ClientMonitor.get_instance()
        monitor.start()
    except Exception as e:
        logger.error("Failed to initialize ClientMonitor: %s", e)


if __name__ == "__main__":
    # Test the monitor
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    monitor = ClientMonitor.get_instance()
    monitor.start()
    
    print("\n👁️  Client Monitor Test")
    print("=" * 50)
    
    # Simulate connections
    print("\nSimulating local connection...")
    monitor.client_connected("client1", "127.0.0.1", "Mozilla/5.0")
    time.sleep(1)
    
    print("\nSimulating remote connection...")
    monitor.client_connected("client2", "93.45.123.45", "Mozilla/5.0")
    time.sleep(1)
    
    print("\nCurrent status:")
    status = monitor.get_status()
    print(json.dumps(status, indent=2))
    
    print("\nClients info:")
    for client in monitor.get_clients_info():
        print(f"  - {client['client_id']}: {client['ip']} ({'LOCAL' if client['is_local'] else 'REMOTE'})")
    
    print("\nDisconnecting remote client...")
    monitor.client_disconnected("client2")
    time.sleep(1)
    
    print("\nFinal status:")
    status = monitor.get_status()
    print(json.dumps(status, indent=2))
    
    monitor.stop()
    print("\n✅ Test completed!")
