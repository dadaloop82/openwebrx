"""
Auto Mode Orchestrator for OpenWebRX
Coordinates all auto-mode components (ClientMonitor, AutoTuner, DecoderManager, AutoRecorder)
Implements the state machine and schedules automatic frequency scanning
"""

import os
import logging
import threading
import time
import json
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Any
from enum import Enum

logger = logging.getLogger(__name__)


class AutoModeState(Enum):
    """Auto mode operational states"""
    MANUAL = "manual"  # User has control
    IDLE = "idle"  # No clients, but not scanning
    AUTO = "auto"  # Actively scanning and recording


class AutoModeOrchestrator:
    """Orchestrates auto-mode operation"""
    
    instance = None
    lock = threading.Lock()
    
    @staticmethod
    def get_instance():
        with AutoModeOrchestrator.lock:
            if AutoModeOrchestrator.instance is None:
                AutoModeOrchestrator.instance = AutoModeOrchestrator()
        return AutoModeOrchestrator.instance
    
    def __init__(self):
        self.state = AutoModeState.MANUAL
        self.config = self._load_config()
        self.running = False
        self.orchestrator_thread = None
        
        # Components
        self.client_monitor = None
        self.auto_tuner = None
        self.decoder_manager = None
        self.auto_recorder = None
        
        # Current operation
        self.current_frequency_index = 0
        self.frequencies = []
        self.saved_user_settings = None
        
        logger.info("AutoModeOrchestrator initialized")
    
    def _load_config(self) -> Dict:
        """Load configuration"""
        default_config = {
            'enabled': True,
            'frequencies': [
                {
                    'frequency': 145800000,
                    'mode': 'NFM',
                    'squelch': 0.15,
                    'bandwidth': 12500,
                    'dwell_time': 60,
                    'label': 'APRS 2m'
                },
                {
                    'frequency': 14074000,
                    'mode': 'USB',
                    'squelch': 0.0,
                    'bandwidth': 2500,
                    'dwell_time': 120,
                    'label': 'FT8 20m'
                },
                {
                    'frequency': 144800000,
                    'mode': 'NFM',
                    'squelch': 0.20,
                    'bandwidth': 12500,
                    'dwell_time': 60,
                    'label': 'Calling Channel'
                }
            ],
            'cycle_mode': 'sequential',  # 'sequential' or 'priority'
            'enable_recording': True,
            'enable_decoders': True,
            'transition_delay': 2  # seconds to wait when switching modes
        }
        
        try:
            from owrx.config.core import CoreConfig
            config_file = os.path.join(
                CoreConfig().get_data_directory(),
                'auto_mode_config.json'
            )
            
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    data = json.load(f)
                    return data.get('orchestrator', default_config)
        except Exception as e:
            logger.debug("Using default orchestrator config: %s", e)
        
        return default_config
    
    def set_components(self, client_monitor=None, auto_tuner=None, 
                      decoder_manager=None, auto_recorder=None):
        """Set component instances"""
        if client_monitor:
            self.client_monitor = client_monitor
            # Register callbacks
            client_monitor.register_callback('all_remote_clients_gone', 
                                            self._on_clients_gone)
            client_monitor.register_callback('remote_client_connected', 
                                            self._on_client_connected)
        
        if auto_tuner:
            self.auto_tuner = auto_tuner
        
        if decoder_manager:
            self.decoder_manager = decoder_manager
        
        if auto_recorder:
            self.auto_recorder = auto_recorder
        
        logger.info("Components registered with orchestrator")
    
    def start(self):
        """Start the orchestrator"""
        if not self.config['enabled']:
            logger.info("AutoModeOrchestrator disabled in config")
            return
        
        if self.running:
            logger.warning("AutoModeOrchestrator already running")
            return
        
        # Load frequencies from config
        self.frequencies = self.config.get('frequencies', [])
        if not self.frequencies:
            logger.error("No frequencies configured for auto-mode!")
            return
        
        self.running = True
        self.orchestrator_thread = threading.Thread(
            target=self._orchestrator_loop, 
            daemon=True
        )
        self.orchestrator_thread.start()
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("🎼 AUTO MODE ORCHESTRATOR STARTED")
        logger.info("   Frequencies: %d", len(self.frequencies))
        logger.info("   Cycle mode: %s", self.config['cycle_mode'])
        logger.info("═══════════════════════════════════════════════════")
    
    def stop(self):
        """Stop the orchestrator"""
        self.running = False
        
        # Exit auto mode if active
        if self.state == AutoModeState.AUTO:
            self._exit_auto_mode()
        
        if self.orchestrator_thread:
            self.orchestrator_thread.join(timeout=10)
        
        logger.info("AutoModeOrchestrator stopped")
    
    def _on_clients_gone(self):
        """Callback when all remote clients disconnect"""
        if self.state == AutoModeState.MANUAL:
            logger.info("🎯 Clients gone - transitioning to AUTO mode")
            self._enter_auto_mode()
    
    def _on_client_connected(self, client):
        """Callback when a remote client connects"""
        if self.state == AutoModeState.AUTO:
            logger.info("👤 Remote client connected - exiting AUTO mode")
            self._exit_auto_mode()
    
    def _enter_auto_mode(self):
        """Enter automatic mode"""
        try:
            # Save current user settings
            if self.auto_tuner:
                self.saved_user_settings = self.auto_tuner.get_current_settings()
            
            # Change state
            self.state = AutoModeState.AUTO
            
            # Notify components
            if self.auto_tuner:
                self.auto_tuner.enter_auto_mode()
            
            # Reset frequency index
            self.current_frequency_index = 0
            
            logger.info("═══════════════════════════════════════════════════")
            logger.info("🤖 ENTERED AUTO MODE")
            logger.info("   Will cycle through %d frequencies", len(self.frequencies))
            logger.info("═══════════════════════════════════════════════════")
            
        except Exception as e:
            logger.error("Error entering auto mode: %s", e, exc_info=True)
            self.state = AutoModeState.MANUAL
    
    def _exit_auto_mode(self):
        """Exit automatic mode"""
        try:
            # Change state first
            old_state = self.state
            self.state = AutoModeState.MANUAL
            
            if old_state != AutoModeState.AUTO:
                return
            
            # Stop recording session
            if self.decoder_manager:
                self.decoder_manager.stop_session()
            
            # Stop auto recorder if running
            if self.auto_recorder:
                try:
                    if hasattr(self.auto_recorder, 'stop_recording'):
                        self.auto_recorder.stop_recording()
                except:
                    pass
            
            # Restore user settings
            if self.auto_tuner and self.saved_user_settings:
                self.auto_tuner.restore_settings(self.saved_user_settings)
                self.saved_user_settings = None
            
            # Notify components
            if self.auto_tuner:
                self.auto_tuner.exit_auto_mode()
            
            logger.info("═══════════════════════════════════════════════════")
            logger.info("👤 EXITED AUTO MODE - User control restored")
            logger.info("═══════════════════════════════════════════════════")
            
        except Exception as e:
            logger.error("Error exiting auto mode: %s", e, exc_info=True)
    
    def _orchestrator_loop(self):
        """Main orchestrator loop"""
        while self.running:
            try:
                if self.state == AutoModeState.AUTO:
                    self._handle_auto_mode()
                else:
                    # Just idle
                    time.sleep(1)
                    
            except Exception as e:
                logger.error("Error in orchestrator loop: %s", e, exc_info=True)
                time.sleep(5)
    
    def _handle_auto_mode(self):
        """Handle auto mode operation"""
        try:
            # Get current frequency config
            if not self.frequencies:
                logger.warning("No frequencies configured")
                time.sleep(10)
                return
            
            freq_config = self.frequencies[self.current_frequency_index]
            
            logger.info("═══════════════════════════════════════════════════")
            logger.info("📡 Scanning: %s", freq_config.get('label', 'Unknown'))
            logger.info("   Frequency: %.3f MHz", freq_config['frequency'] / 1e6)
            logger.info("   Mode: %s", freq_config['mode'])
            logger.info("   Dwell time: %ds", freq_config['dwell_time'])
            logger.info("═══════════════════════════════════════════════════")
            
            # Tune to frequency
            if self.auto_tuner:
                success = self.auto_tuner.tune_frequency(
                    frequency=freq_config['frequency'],
                    mode=freq_config['mode'],
                    squelch=freq_config.get('squelch'),
                    bandwidth=freq_config.get('bandwidth')
                )
                
                if not success:
                    logger.error("Failed to tune to frequency")
                    time.sleep(5)
                    return
            
            # Wait for transition
            time.sleep(self.config['transition_delay'])
            
            # Start decoder session
            if self.decoder_manager and self.config['enable_decoders']:
                self.decoder_manager.start_session(
                    freq_config['frequency'],
                    freq_config['mode']
                )
            
            # Start recording if enabled
            if self.auto_recorder and self.config['enable_recording']:
                try:
                    if hasattr(self.auto_recorder, 'start_recording'):
                        self.auto_recorder.start_recording()
                except Exception as e:
                    logger.error("Error starting recorder: %s", e)
            
            # Dwell on this frequency
            dwell_time = freq_config.get('dwell_time', 60)
            dwell_end = time.time() + dwell_time
            
            while time.time() < dwell_end and self.state == AutoModeState.AUTO:
                time.sleep(1)
            
            # Stop recording
            if self.auto_recorder and self.config['enable_recording']:
                try:
                    if hasattr(self.auto_recorder, 'stop_recording'):
                        self.auto_recorder.stop_recording()
                except Exception as e:
                    logger.error("Error stopping recorder: %s", e)
            
            # Stop decoder session
            if self.decoder_manager:
                self.decoder_manager.stop_session()
            
            # Move to next frequency
            if self.state == AutoModeState.AUTO:  # Check we're still in auto mode
                self.current_frequency_index = (
                    self.current_frequency_index + 1
                ) % len(self.frequencies)
            
        except Exception as e:
            logger.error("Error in auto mode handler: %s", e, exc_info=True)
            time.sleep(5)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current orchestrator status"""
        current_freq = None
        if (self.state == AutoModeState.AUTO and 
            self.frequencies and 
            self.current_frequency_index < len(self.frequencies)):
            current_freq = self.frequencies[self.current_frequency_index]
        
        return {
            'enabled': self.config['enabled'],
            'running': self.running,
            'state': self.state.value,
            'current_frequency': current_freq,
            'total_frequencies': len(self.frequencies),
            'components': {
                'client_monitor': self.client_monitor is not None,
                'auto_tuner': self.auto_tuner is not None,
                'decoder_manager': self.decoder_manager is not None,
                'auto_recorder': self.auto_recorder is not None
            }
        }
    
    def force_enter_auto_mode(self):
        """Force enter auto mode (for testing)"""
        if self.state == AutoModeState.MANUAL:
            self._enter_auto_mode()
    
    def force_exit_auto_mode(self):
        """Force exit auto mode"""
        if self.state == AutoModeState.AUTO:
            self._exit_auto_mode()


def init_orchestrator():
    """Initialize the orchestrator"""
    try:
        orchestrator = AutoModeOrchestrator.get_instance()
        orchestrator.start()
    except Exception as e:
        logger.error("Failed to initialize orchestrator: %s", e)


if __name__ == "__main__":
    # Test the orchestrator
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    orchestrator = AutoModeOrchestrator.get_instance()
    orchestrator.start()
    
    print("\n🎼 Orchestrator Test")
    print("=" * 50)
    
    print("\nCurrent status:")
    status = orchestrator.get_status()
    print(json.dumps(status, indent=2))
    
    print("\nForcing auto mode for 10 seconds...")
    orchestrator.force_enter_auto_mode()
    time.sleep(10)
    
    print("\nExiting auto mode...")
    orchestrator.force_exit_auto_mode()
    
    print("\nFinal status:")
    status = orchestrator.get_status()
    print(json.dumps(status, indent=2))
    
    orchestrator.stop()
    print("\n✅ Test completed!")
