"""
Decoder Manager for OpenWebRX
Manages all digital decoders (DMR, APRS, FT8, POCSAG, ADS-B, etc.)
Captures and saves decodings during auto-mode operation
"""

import os
import logging
import threading
import time
import json
import csv
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class DecoderManager:
    """Manages digital decoders and captures decodings"""
    
    instance = None
    lock = threading.Lock()
    
    # Supported decoders and their typical modes
    DECODER_MODES = {
        'dmr': ['DMR'],
        'ysf': ['YSF'],
        'nxdn': ['NXDN'],
        'dstar': ['D-Star'],
        'm17': ['M17'],
        'aprs': ['APRS'],
        'ft8': ['FT8'],
        'ft4': ['FT4'],
        'wspr': ['WSPR'],
        'pocsag': ['POCSAG'],
        'adsb': ['ADS-B'],
        'acars': ['ACARS'],
        'rtty': ['RTTY'],
        'psk': ['PSK31', 'PSK63'],
        'packet': ['Packet']
    }
    
    @staticmethod
    def get_instance():
        with DecoderManager.lock:
            if DecoderManager.instance is None:
                DecoderManager.instance = DecoderManager()
        return DecoderManager.instance
    
    def __init__(self):
        self.decoders: Dict[str, Any] = {}
        self.active_decoders: Dict[str, bool] = {}
        self.decodings: List[Dict] = []
        self.decodings_lock = threading.Lock()
        self.config = self._load_config()
        self.output_dir = self._get_output_directory()
        self.current_session_id = None
        self.is_recording = False
        
        # Statistics
        self.stats = defaultdict(int)
        
        logger.info("DecoderManager initialized")
    
    def _load_config(self) -> Dict:
        """Load configuration"""
        default_config = {
            'enabled': True,
            'save_decodings': True,
            'save_format': 'both',  # 'json', 'csv', or 'both'
            'buffer_size': 100,
            'flush_interval_seconds': 5,
            'enabled_decoders': list(self.DECODER_MODES.keys())
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
                    return data.get('decoder_manager', default_config)
        except Exception as e:
            logger.debug("Using default decoder config: %s", e)
        
        return default_config
    
    def _get_output_directory(self) -> str:
        """Get the output directory for decodings"""
        try:
            from owrx.config.core import CoreConfig
            base_dir = CoreConfig().get_data_directory()
        except:
            base_dir = '/var/lib/openwebrx'
        
        output_dir = os.path.join(base_dir, 'auto_mode_decodings')
        os.makedirs(output_dir, exist_ok=True)
        return output_dir
    
    def start_session(self, frequency: int, mode: str):
        """Start a new decoding session"""
        self.is_recording = True
        self.current_session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        session_dir = os.path.join(
            self.output_dir,
            self.current_session_id
        )
        os.makedirs(session_dir, exist_ok=True)
        
        # Write session metadata
        metadata = {
            'session_id': self.current_session_id,
            'start_time': datetime.now().isoformat(),
            'frequency': frequency,
            'mode': mode,
            'enabled_decoders': self.config['enabled_decoders']
        }
        
        metadata_file = os.path.join(session_dir, 'session.json')
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("📡 DECODING SESSION STARTED")
        logger.info("   Session ID: %s", self.current_session_id)
        logger.info("   Frequency: %.3f MHz", frequency / 1e6)
        logger.info("   Mode: %s", mode)
        logger.info("   Output: %s", session_dir)
        logger.info("═══════════════════════════════════════════════════")
    
    def stop_session(self):
        """Stop current decoding session"""
        if not self.is_recording:
            return
        
        self.is_recording = False
        
        # Flush any remaining decodings
        self._flush_decodings()
        
        # Write final statistics
        if self.current_session_id:
            session_dir = os.path.join(self.output_dir, self.current_session_id)
            stats_file = os.path.join(session_dir, 'statistics.json')
            
            stats_data = {
                'end_time': datetime.now().isoformat(),
                'total_decodings': sum(self.stats.values()),
                'by_decoder': dict(self.stats)
            }
            
            with open(stats_file, 'w') as f:
                json.dump(stats_data, f, indent=2)
            
            logger.info("═══════════════════════════════════════════════════")
            logger.info("📡 DECODING SESSION ENDED")
            logger.info("   Session ID: %s", self.current_session_id)
            logger.info("   Total decodings: %d", stats_data['total_decodings'])
            for decoder, count in self.stats.items():
                if count > 0:
                    logger.info("   - %s: %d", decoder, count)
            logger.info("═══════════════════════════════════════════════════")
        
        # Reset stats
        self.stats = defaultdict(int)
        self.current_session_id = None
    
    def register_decoder(self, decoder_type: str, decoder_instance: Any):
        """Register a decoder instance"""
        self.decoders[decoder_type] = decoder_instance
        self.active_decoders[decoder_type] = False
        logger.info("Registered decoder: %s", decoder_type)
    
    def enable_decoder(self, decoder_type: str) -> bool:
        """Enable a specific decoder"""
        if decoder_type not in self.decoders:
            logger.warning("Decoder not registered: %s", decoder_type)
            return False
        
        try:
            decoder = self.decoders[decoder_type]
            
            # Try different enable methods
            if hasattr(decoder, 'enable'):
                decoder.enable()
            elif hasattr(decoder, 'start'):
                decoder.start()
            elif hasattr(decoder, 'set_enabled'):
                decoder.set_enabled(True)
            
            self.active_decoders[decoder_type] = True
            logger.info("Enabled decoder: %s", decoder_type)
            return True
            
        except Exception as e:
            logger.error("Error enabling decoder %s: %s", decoder_type, e)
            return False
    
    def disable_decoder(self, decoder_type: str) -> bool:
        """Disable a specific decoder"""
        if decoder_type not in self.decoders:
            return False
        
        try:
            decoder = self.decoders[decoder_type]
            
            # Try different disable methods
            if hasattr(decoder, 'disable'):
                decoder.disable()
            elif hasattr(decoder, 'stop'):
                decoder.stop()
            elif hasattr(decoder, 'set_enabled'):
                decoder.set_enabled(False)
            
            self.active_decoders[decoder_type] = False
            logger.info("Disabled decoder: %s", decoder_type)
            return True
            
        except Exception as e:
            logger.error("Error disabling decoder %s: %s", decoder_type, e)
            return False
    
    def add_decoding(self, decoder_type: str, decoding_data: Dict[str, Any]):
        """Add a new decoding result"""
        if not self.is_recording:
            return
        
        with self.decodings_lock:
            # Add metadata
            decoding = {
                'timestamp': datetime.now().isoformat(),
                'session_id': self.current_session_id,
                'decoder': decoder_type,
                **decoding_data
            }
            
            self.decodings.append(decoding)
            self.stats[decoder_type] += 1
            
            # Flush if buffer is full
            if len(self.decodings) >= self.config['buffer_size']:
                self._flush_decodings()
            
            # Log interesting decodings
            if decoder_type in ['dmr', 'ysf', 'nxdn', 'dstar', 'm17']:
                source = decoding_data.get('source', 'Unknown')
                logger.info("📻 %s: %s", decoder_type.upper(), source)
            elif decoder_type in ['aprs']:
                callsign = decoding_data.get('callsign', 'Unknown')
                logger.info("📍 APRS: %s", callsign)
            elif decoder_type in ['ft8', 'ft4']:
                callsign = decoding_data.get('callsign', 'Unknown')
                logger.info("📡 %s: %s", decoder_type.upper(), callsign)
            elif decoder_type in ['pocsag']:
                address = decoding_data.get('address', 'Unknown')
                logger.info("📟 POCSAG: %s", address)
            elif decoder_type in ['adsb']:
                icao = decoding_data.get('icao', 'Unknown')
                logger.info("✈️  ADS-B: %s", icao)
    
    def _flush_decodings(self):
        """Flush buffered decodings to disk"""
        with self.decodings_lock:
            if not self.decodings or not self.current_session_id:
                return
            
            session_dir = os.path.join(self.output_dir, self.current_session_id)
            
            try:
                # Save as JSON
                if self.config['save_format'] in ['json', 'both']:
                    json_file = os.path.join(session_dir, 'decodings.json')
                    
                    # Append to existing file or create new
                    existing_data = []
                    if os.path.exists(json_file):
                        with open(json_file, 'r') as f:
                            existing_data = json.load(f)
                    
                    existing_data.extend(self.decodings)
                    
                    with open(json_file, 'w') as f:
                        json.dump(existing_data, f, indent=2)
                
                # Save as CSV
                if self.config['save_format'] in ['csv', 'both']:
                    csv_file = os.path.join(session_dir, 'decodings.csv')
                    
                    # Determine if we need to write header
                    write_header = not os.path.exists(csv_file)
                    
                    with open(csv_file, 'a', newline='') as f:
                        if self.decodings:
                            # Get all unique keys from all decodings
                            all_keys = set()
                            for d in self.decodings:
                                all_keys.update(d.keys())
                            
                            writer = csv.DictWriter(f, fieldnames=sorted(all_keys))
                            
                            if write_header:
                                writer.writeheader()
                            
                            writer.writerows(self.decodings)
                
                logger.debug("Flushed %d decodings to disk", len(self.decodings))
                
            except Exception as e:
                logger.error("Error flushing decodings: %s", e)
            
            finally:
                # Clear buffer
                self.decodings = []
    
    def get_active_decoders(self) -> List[str]:
        """Get list of currently active decoders"""
        return [dt for dt, active in self.active_decoders.items() if active]
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get current statistics"""
        return {
            'is_recording': self.is_recording,
            'current_session': self.current_session_id,
            'total_decodings': sum(self.stats.values()),
            'by_decoder': dict(self.stats),
            'active_decoders': self.get_active_decoders(),
            'buffered_decodings': len(self.decodings)
        }


def init_decoder_manager():
    """Initialize the decoder manager"""
    try:
        manager = DecoderManager.get_instance()
        logger.info("DecoderManager ready")
    except Exception as e:
        logger.error("Failed to initialize DecoderManager: %s", e)


if __name__ == "__main__":
    # Test the decoder manager
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    manager = DecoderManager.get_instance()
    
    print("\n📡 Decoder Manager Test")
    print("=" * 50)
    
    # Start a test session
    print("\nStarting test session...")
    manager.start_session(145800000, "NFM")
    time.sleep(1)
    
    # Simulate some decodings
    print("\nSimulating decodings...")
    manager.add_decoding('aprs', {
        'callsign': 'N0CALL',
        'latitude': 45.5,
        'longitude': -122.6,
        'comment': 'Test packet'
    })
    
    manager.add_decoding('dmr', {
        'source': '1234567',
        'destination': '7654321',
        'talkgroup': 'TG91',
        'slot': 1
    })
    
    manager.add_decoding('ft8', {
        'callsign': 'K1ABC',
        'grid': 'FN42',
        'snr': 5
    })
    
    time.sleep(1)
    
    # Get statistics
    print("\nCurrent statistics:")
    stats = manager.get_statistics()
    print(json.dumps(stats, indent=2))
    
    # Stop session
    print("\nStopping session...")
    manager.stop_session()
    
    print("\n✅ Test completed!")
    print(f"Output directory: {manager.output_dir}")
