"""
Auto Mode Initialization
Initializes all auto-mode components when OpenWebRX starts
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Global instances
_client_monitor = None
_auto_tuner = None
_decoder_manager = None
_orchestrator = None
_init_lock = threading.Lock()
_initialized = False


def init_auto_mode_system(receiver=None):
    """
    Initialize the complete auto-mode system
    
    Args:
        receiver: Optional receiver instance to attach to AutoTuner
    """
    global _client_monitor, _auto_tuner, _decoder_manager, _orchestrator, _initialized
    
    with _init_lock:
        if _initialized:
            logger.warning("Auto-mode system already initialized")
            return
        
        try:
            logger.info("═══════════════════════════════════════════════════")
            logger.info("🚀 Initializing AUTO MODE SYSTEM")
            logger.info("═══════════════════════════════════════════════════")
            
            # Import components
            from owrx.client_monitor import ClientMonitor
            from owrx.auto_tuner import AutoTuner
            from owrx.decoder_manager import DecoderManager
            from owrx.auto_mode_orchestrator import AutoModeOrchestrator
            
            # Initialize components
            logger.info("⚙️  Initializing ClientMonitor...")
            _client_monitor = ClientMonitor.get_instance()
            _client_monitor.start()
            
            logger.info("⚙️  Initializing AutoTuner...")
            _auto_tuner = AutoTuner.get_instance()
            if receiver:
                _auto_tuner.set_receiver(receiver)
            
            logger.info("⚙️  Initializing DecoderManager...")
            _decoder_manager = DecoderManager.get_instance()
            
            logger.info("⚙️  Initializing Orchestrator...")
            _orchestrator = AutoModeOrchestrator.get_instance()
            _orchestrator.set_components(
                client_monitor=_client_monitor,
                auto_tuner=_auto_tuner,
                decoder_manager=_decoder_manager,
                auto_recorder=None  # Will be set later if needed
            )
            _orchestrator.start()
            
            _initialized = True
            
            logger.info("═══════════════════════════════════════════════════")
            logger.info("✅ AUTO MODE SYSTEM INITIALIZED")
            logger.info("   🔹 ClientMonitor: Active")
            logger.info("   🔹 AutoTuner: Ready")
            logger.info("   🔹 DecoderManager: Ready")
            logger.info("   🔹 Orchestrator: Running")
            logger.info("═══════════════════════════════════════════════════")
            
        except Exception as e:
            logger.error("Failed to initialize auto-mode system: %s", e, exc_info=True)
            _initialized = False


def notify_client_connected(client_id: str, ip_address: str, user_agent: str = ""):
    """
    Notify the system that a client has connected
    
    Args:
        client_id: Unique client identifier
        ip_address: Client IP address
        user_agent: Client user agent string
    """
    if _client_monitor:
        _client_monitor.client_connected(client_id, ip_address, user_agent)
    else:
        logger.debug("ClientMonitor not initialized, ignoring connection")


def notify_client_disconnected(client_id: str):
    """
    Notify the system that a client has disconnected
    
    Args:
        client_id: Unique client identifier
    """
    if _client_monitor:
        _client_monitor.client_disconnected(client_id)
    else:
        logger.debug("ClientMonitor not initialized, ignoring disconnection")


def notify_client_activity(client_id: str):
    """
    Notify the system of client activity
    
    Args:
        client_id: Unique client identifier
    """
    if _client_monitor:
        _client_monitor.client_activity(client_id)


def get_auto_mode_status():
    """Get status of all auto-mode components"""
    status = {
        'initialized': _initialized,
        'client_monitor': None,
        'orchestrator': None
    }
    
    if _client_monitor:
        status['client_monitor'] = _client_monitor.get_status()
    
    if _orchestrator:
        status['orchestrator'] = _orchestrator.get_status()
    
    if _decoder_manager:
        status['decoder_manager'] = _decoder_manager.get_statistics()
    
    return status


def shutdown_auto_mode_system():
    """Shutdown the auto-mode system"""
    global _initialized
    
    logger.info("Shutting down auto-mode system...")
    
    if _orchestrator:
        _orchestrator.stop()
    
    if _decoder_manager:
        _decoder_manager.stop_session()
    
    if _client_monitor:
        _client_monitor.stop()
    
    _initialized = False
    logger.info("Auto-mode system shutdown complete")


# Export singleton instances for direct access if needed
def get_client_monitor():
    return _client_monitor


def get_auto_tuner():
    return _auto_tuner


def get_decoder_manager():
    return _decoder_manager


def get_orchestrator():
    return _orchestrator


if __name__ == "__main__":
    # Test initialization
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    print("\n🚀 Testing Auto Mode Initialization")
    print("=" * 70)
    
    init_auto_mode_system()
    
    import time
    time.sleep(2)
    
    print("\n📊 System Status:")
    import json
    status = get_auto_mode_status()
    print(json.dumps(status, indent=2, default=str))
    
    print("\n🧪 Simulating client connections...")
    notify_client_connected("test_client_1", "127.0.0.1", "Test Agent")
    time.sleep(1)
    
    notify_client_connected("test_client_2", "93.45.123.45", "Remote Agent")
    time.sleep(1)
    
    print("\n📊 Status with clients:")
    status = get_auto_mode_status()
    print(json.dumps(status, indent=2, default=str))
    
    print("\n🔌 Disconnecting remote client...")
    notify_client_disconnected("test_client_2")
    time.sleep(2)
    
    print("\n📊 Final status:")
    status = get_auto_mode_status()
    print(json.dumps(status, indent=2, default=str))
    
    print("\n🛑 Shutting down...")
    shutdown_auto_mode_system()
    
    print("\n✅ Test completed!")
