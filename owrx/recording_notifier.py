"""
Recording status notifier for WebSocket clients
"""

import threading
import time
import logging
from typing import Set

logger = logging.getLogger(__name__)


class RecordingNotifier:
    """Notifies connected clients about recording status"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self.handlers = set()
        self.is_recording = False
        self.current_frequency = None
        self._initialized = True
    
    def register_handler(self, handler):
        """Register a connection handler to receive notifications"""
        with self._lock:
            self.handlers.add(handler)
    
    def unregister_handler(self, handler):
        """Unregister a connection handler"""
        with self._lock:
            self.handlers.discard(handler)
    
    def notify_recording_start(self, frequency_hz: int):
        """Notify all clients that recording has started"""
        self.is_recording = True
        self.current_frequency = frequency_hz
        
        message = {
            'type': 'recording_status',
            'recording': True,
            'frequency': frequency_hz
        }
        
        self._broadcast(message)
        logger.info("📼 Broadcasting: Recording started at %.3f MHz", frequency_hz / 1e6)
    
    def notify_recording_stop(self):
        """Notify all clients that recording has stopped"""
        self.is_recording = False
        
        message = {
            'type': 'recording_status',
            'recording': False
        }
        
        self._broadcast(message)
        logger.info("⏹️  Broadcasting: Recording stopped")
    
    def _broadcast(self, message):
        """Send message to all registered handlers"""
        with self._lock:
            dead_handlers = set()
            
            for handler in self.handlers:
                try:
                    if hasattr(handler, 'write_recording_status'):
                        handler.write_recording_status(message)
                except Exception as e:
                    logger.debug("Handler error, marking for removal: %s", e)
                    dead_handlers.add(handler)
            
            # Clean up dead handlers
            for handler in dead_handlers:
                self.handlers.discard(handler)


_notifier = None

def get_notifier() -> RecordingNotifier:
    """Get global notifier instance"""
    global _notifier
    if _notifier is None:
        _notifier = RecordingNotifier()
    return _notifier
