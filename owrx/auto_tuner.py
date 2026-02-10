"""
Automatic Tuner for OpenWebRX
Controls receiver frequency, mode, squelch, and bandwidth programmatically
Used by auto-mode to switch between frequencies automatically
"""

import os
import logging
import threading
import time
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class AutoTuner:
    """Controls the receiver to automatically tune frequencies"""
    
    instance = None
    lock = threading.Lock()
    
    @staticmethod
    def get_instance():
        with AutoTuner.lock:
            if AutoTuner.instance is None:
                AutoTuner.instance = AutoTuner()
        return AutoTuner.instance
    
    def __init__(self):
        self.receiver = None
        self.current_frequency = None
        self.current_mode = None
        self.current_squelch = None
        self.current_bandwidth = None
        self.tuner_lock = threading.Lock()
        self.is_auto_mode = False
        
        logger.info("AutoTuner initialized")
    
    def set_receiver(self, receiver):
        """Set the receiver instance to control"""
        self.receiver = receiver
        logger.info("AutoTuner attached to receiver")
    
    def get_receiver_status(self) -> Dict[str, Any]:
        """Get current receiver status"""
        if not self.receiver:
            return {
                'connected': False,
                'error': 'No receiver attached'
            }
        
        try:
            # Try to get receiver properties
            props = {}
            
            # Get frequency if available
            if hasattr(self.receiver, 'getFrequency'):
                props['frequency'] = self.receiver.getFrequency()
            elif hasattr(self.receiver, 'profile'):
                props['frequency'] = getattr(self.receiver.profile, 'center_freq', None)
            
            # Get demodulator if available
            if hasattr(self.receiver, 'getDm'):
                dm = self.receiver.getDm()
                if dm:
                    props['mode'] = getattr(dm, 'get_mode', lambda: None)()
                    props['squelch'] = getattr(dm, 'get_squelch_level', lambda: None)()
            
            return {
                'connected': True,
                **props
            }
        except Exception as e:
            logger.error("Error getting receiver status: %s", e)
            return {
                'connected': True,
                'error': str(e)
            }
    
    def tune_frequency(self, frequency: int, mode: str = None, 
                      squelch: float = None, bandwidth: int = None) -> bool:
        """
        Tune to a specific frequency with optional parameters
        
        Args:
            frequency: Frequency in Hz
            mode: Demodulation mode (USB, LSB, AM, FM, NFM, WFM, etc.)
            squelch: Squelch level (0.0 to 1.0)
            bandwidth: Bandwidth in Hz
        
        Returns:
            True if successful, False otherwise
        """
        with self.tuner_lock:
            if not self.receiver:
                logger.error("Cannot tune: No receiver attached")
                return False
            
            try:
                logger.info("🎯 Tuning to %.3f MHz (mode=%s, squelch=%s, bw=%s)",
                           frequency / 1e6, mode, squelch, bandwidth)
                
                # Store current settings
                old_freq = self.current_frequency
                old_mode = self.current_mode
                
                # Tune frequency
                success = self._set_frequency(frequency)
                if not success:
                    logger.error("Failed to set frequency")
                    return False
                
                self.current_frequency = frequency
                
                # Set mode if specified
                if mode:
                    if self._set_mode(mode):
                        self.current_mode = mode
                    else:
                        logger.warning("Failed to set mode: %s", mode)
                
                # Set squelch if specified
                if squelch is not None:
                    if self._set_squelch(squelch):
                        self.current_squelch = squelch
                    else:
                        logger.warning("Failed to set squelch: %s", squelch)
                
                # Set bandwidth if specified
                if bandwidth:
                    if self._set_bandwidth(bandwidth):
                        self.current_bandwidth = bandwidth
                    else:
                        logger.warning("Failed to set bandwidth: %s", bandwidth)
                
                logger.info("✅ Successfully tuned to %.3f MHz", frequency / 1e6)
                return True
                
            except Exception as e:
                logger.error("Error tuning frequency: %s", e, exc_info=True)
                return False
    
    def _set_frequency(self, frequency: int) -> bool:
        """Internal method to set frequency"""
        try:
            # Method 1: Direct setFrequency
            if hasattr(self.receiver, 'setFrequency'):
                self.receiver.setFrequency(frequency)
                return True
            
            # Method 2: Through profile
            if hasattr(self.receiver, 'profile'):
                profile = self.receiver.profile
                if hasattr(profile, 'center_freq'):
                    profile.center_freq = frequency
                    return True
            
            # Method 3: Through demodulator
            if hasattr(self.receiver, 'getDm'):
                dm = self.receiver.getDm()
                if dm and hasattr(dm, 'set_frequency'):
                    dm.set_frequency(frequency)
                    return True
            
            logger.error("No method available to set frequency")
            return False
            
        except Exception as e:
            logger.error("Error setting frequency: %s", e)
            return False
    
    def _set_mode(self, mode: str) -> bool:
        """Internal method to set demodulation mode"""
        try:
            # Normalize mode name
            mode = mode.upper()
            
            # Method 1: Through demodulator
            if hasattr(self.receiver, 'getDm'):
                dm = self.receiver.getDm()
                if dm and hasattr(dm, 'set_mode'):
                    dm.set_mode(mode)
                    return True
            
            # Method 2: Through setDemodulator
            if hasattr(self.receiver, 'setDemodulator'):
                self.receiver.setDemodulator(mode)
                return True
            
            logger.error("No method available to set mode")
            return False
            
        except Exception as e:
            logger.error("Error setting mode: %s", e)
            return False
    
    def _set_squelch(self, squelch: float) -> bool:
        """Internal method to set squelch level"""
        try:
            # Clamp squelch to valid range
            squelch = max(0.0, min(1.0, squelch))
            
            # Method 1: Through demodulator
            if hasattr(self.receiver, 'getDm'):
                dm = self.receiver.getDm()
                if dm and hasattr(dm, 'set_squelch_level'):
                    dm.set_squelch_level(squelch)
                    return True
            
            # Method 2: Through setSquelch
            if hasattr(self.receiver, 'setSquelch'):
                self.receiver.setSquelch(squelch)
                return True
            
            logger.error("No method available to set squelch")
            return False
            
        except Exception as e:
            logger.error("Error setting squelch: %s", e)
            return False
    
    def _set_bandwidth(self, bandwidth: int) -> bool:
        """Internal method to set bandwidth"""
        try:
            # Method 1: Through demodulator
            if hasattr(self.receiver, 'getDm'):
                dm = self.receiver.getDm()
                if dm and hasattr(dm, 'set_bandwidth'):
                    dm.set_bandwidth(bandwidth)
                    return True
            
            # Method 2: Through setBandwidth
            if hasattr(self.receiver, 'setBandwidth'):
                self.receiver.setBandwidth(bandwidth)
                return True
            
            logger.error("No method available to set bandwidth")
            return False
            
        except Exception as e:
            logger.error("Error setting bandwidth: %s", e)
            return False
    
    def get_current_settings(self) -> Dict[str, Any]:
        """Get current tuner settings"""
        return {
            'frequency': self.current_frequency,
            'mode': self.current_mode,
            'squelch': self.current_squelch,
            'bandwidth': self.current_bandwidth,
            'is_auto_mode': self.is_auto_mode
        }
    
    def enter_auto_mode(self):
        """Mark that we're entering auto mode"""
        self.is_auto_mode = True
        logger.info("═══════════════════════════════════════════════════")
        logger.info("🤖 AUTO MODE ACTIVATED")
        logger.info("═══════════════════════════════════════════════════")
    
    def exit_auto_mode(self):
        """Mark that we're exiting auto mode"""
        self.is_auto_mode = False
        logger.info("═══════════════════════════════════════════════════")
        logger.info("👤 MANUAL MODE - User control restored")
        logger.info("═══════════════════════════════════════════════════")
    
    def restore_settings(self, settings: Dict[str, Any]) -> bool:
        """Restore previous settings"""
        try:
            if settings.get('frequency'):
                self.tune_frequency(
                    frequency=settings['frequency'],
                    mode=settings.get('mode'),
                    squelch=settings.get('squelch'),
                    bandwidth=settings.get('bandwidth')
                )
            return True
        except Exception as e:
            logger.error("Error restoring settings: %s", e)
            return False


def init_auto_tuner(receiver=None):
    """Initialize the auto tuner"""
    try:
        tuner = AutoTuner.get_instance()
        if receiver:
            tuner.set_receiver(receiver)
    except Exception as e:
        logger.error("Failed to initialize AutoTuner: %s", e)


if __name__ == "__main__":
    # Test the tuner
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    tuner = AutoTuner.get_instance()
    
    print("\n🎯 Auto Tuner Test")
    print("=" * 50)
    
    # Test without receiver (should fail gracefully)
    print("\nTesting without receiver...")
    result = tuner.tune_frequency(14074000, mode="USB")
    print(f"Result: {result}")
    
    # Test settings storage
    print("\nEntering auto mode...")
    tuner.enter_auto_mode()
    
    print("\nCurrent settings:")
    settings = tuner.get_current_settings()
    for key, value in settings.items():
        print(f"  {key}: {value}")
    
    print("\nExiting auto mode...")
    tuner.exit_auto_mode()
    
    print("\n✅ Test completed!")
