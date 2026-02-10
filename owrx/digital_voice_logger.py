"""
Digital Voice Logger for OpenWebRX
Logs all talkgroup IDs, radio IDs, and callsigns from DMR, YSF, NXDN, D-Star
Stores data in CSV and JSON format for analysis
"""

import os
import csv
import json
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
from owrx.config.core import CoreConfig

logger = logging.getLogger(__name__)


class DigitalVoiceLog:
    """Represents a single digital voice transmission log entry"""
    
    def __init__(self, mode: str, data: Dict):
        self.timestamp = datetime.now()
        self.mode = mode  # DMR, YSF, NXDN, DSTAR, M17
        self.frequency = data.get('frequency', 0)
        
        # Common fields
        self.source = data.get('source', data.get('callsign', 'Unknown'))
        self.destination = data.get('destination', data.get('talkgroup', 'Unknown'))
        self.slot = data.get('slot', data.get('timeslot', None))
        
        # Mode-specific fields
        self.dmr_id = data.get('dmr_id', data.get('source_id', None))
        self.talkgroup_id = data.get('talkgroup_id', data.get('group_id', None))
        self.color_code = data.get('color_code', None)
        
        # Additional metadata
        self.rssi = data.get('rssi', None)
        self.ber = data.get('ber', None)  # Bit Error Rate
        self.duration = data.get('duration', 0)
        self.raw_data = data
    
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'timestamp': self.timestamp.isoformat(),
            'mode': self.mode,
            'frequency': self.frequency,
            'source': self.source,
            'destination': self.destination,
            'slot': self.slot,
            'dmr_id': self.dmr_id,
            'talkgroup_id': self.talkgroup_id,
            'color_code': self.color_code,
            'rssi': self.rssi,
            'ber': self.ber,
            'duration': self.duration
        }
    
    def to_csv_row(self) -> List:
        """Convert to CSV row"""
        return [
            self.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            self.mode,
            self.frequency,
            self.source,
            self.destination,
            self.slot or '',
            self.dmr_id or '',
            self.talkgroup_id or '',
            self.color_code or '',
            self.rssi or '',
            self.ber or '',
            self.duration
        ]


class DigitalVoiceLogger:
    """Manages digital voice logging"""
    
    instance = None
    lock = threading.Lock()
    
    CSV_HEADERS = [
        'Timestamp', 'Mode', 'Frequency', 'Source', 'Destination',
        'Slot', 'DMR_ID', 'Talkgroup_ID', 'Color_Code', 'RSSI', 'BER', 'Duration'
    ]
    
    @staticmethod
    def get_instance():
        with DigitalVoiceLogger.lock:
            if DigitalVoiceLogger.instance is None:
                DigitalVoiceLogger.instance = DigitalVoiceLogger()
        return DigitalVoiceLogger.instance
    
    def __init__(self):
        self.data_dir = os.path.join(CoreConfig().get_data_directory(), 'digital_voice_logs')
        self.current_csv_file = None
        self.current_json_file = None
        self.buffer: List[DigitalVoiceLog] = []
        self.buffer_lock = threading.Lock()
        self.writer_thread = None
        self.running = False
        self.stats = defaultdict(lambda: defaultdict(int))
        
        os.makedirs(self.data_dir, exist_ok=True)
        self._rotate_log_files()
        
        logger.info("DigitalVoiceLogger initialized - logs dir: %s", self.data_dir)
    
    def _rotate_log_files(self):
        """Create new log files for today"""
        today = datetime.now().strftime('%Y%m%d')
        
        self.current_csv_file = os.path.join(self.data_dir, f'digital_voice_{today}.csv')
        self.current_json_file = os.path.join(self.data_dir, f'digital_voice_{today}.json')
        
        # Create CSV with headers if it doesn't exist
        if not os.path.exists(self.current_csv_file):
            with open(self.current_csv_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)
            logger.info("Created new CSV log file: %s", self.current_csv_file)
        
        # Create JSON file if it doesn't exist
        if not os.path.exists(self.current_json_file):
            with open(self.current_json_file, 'w') as f:
                json.dump({'logs': []}, f)
            logger.info("Created new JSON log file: %s", self.current_json_file)
    
    def start(self):
        """Start the logger"""
        if self.running:
            logger.warning("DigitalVoiceLogger already running")
            return
        
        self.running = True
        self.writer_thread = threading.Thread(target=self._writer_worker, daemon=True)
        self.writer_thread.start()
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("📝 DIGITAL VOICE LOGGER STARTED")
        logger.info("   CSV log: %s", self.current_csv_file)
        logger.info("   JSON log: %s", self.current_json_file)
        logger.info("   Supported: DMR, YSF, NXDN, D-Star, M17")
        logger.info("═══════════════════════════════════════════════════")
    
    def stop(self):
        """Stop the logger"""
        self.running = False
        
        # Flush remaining buffer
        self._flush_buffer()
        
        if self.writer_thread:
            self.writer_thread.join(timeout=5)
        
        logger.info("DigitalVoiceLogger stopped")
    
    def log_transmission(self, mode: str, data: Dict):
        """Log a digital voice transmission"""
        try:
            log_entry = DigitalVoiceLog(mode, data)
            
            with self.buffer_lock:
                self.buffer.append(log_entry)
                
                # Update statistics
                self.stats[mode]['total'] += 1
                if log_entry.source != 'Unknown':
                    self.stats[mode]['sources'].add(log_entry.source)
                if log_entry.talkgroup_id:
                    self.stats[mode]['talkgroups'].add(log_entry.talkgroup_id)
            
            logger.debug("Logged %s transmission: %s → %s", 
                        mode, log_entry.source, log_entry.destination)
            
        except Exception as e:
            logger.error("Failed to log transmission: %s", e)
    
    def _writer_worker(self):
        """Background worker that writes buffered logs to disk"""
        last_rotation_day = datetime.now().day
        
        while self.running:
            try:
                # Check if we need to rotate log files (new day)
                current_day = datetime.now().day
                if current_day != last_rotation_day:
                    self._rotate_log_files()
                    last_rotation_day = current_day
                
                # Flush buffer every 5 seconds or when it has > 100 entries
                with self.buffer_lock:
                    should_flush = len(self.buffer) > 100
                
                if should_flush:
                    self._flush_buffer()
                
                time.sleep(5)
                
            except Exception as e:
                logger.error("Error in writer worker: %s", e)
                time.sleep(5)
    
    def _flush_buffer(self):
        """Write buffered logs to files"""
        with self.buffer_lock:
            if not self.buffer:
                return
            
            entries_to_write = self.buffer.copy()
            self.buffer.clear()
        
        try:
            # Write to CSV
            with open(self.current_csv_file, 'a', newline='') as f:
                writer = csv.writer(f)
                for entry in entries_to_write:
                    writer.writerow(entry.to_csv_row())
            
            # Write to JSON (append to logs array)
            with open(self.current_json_file, 'r') as f:
                data = json.load(f)
            
            data['logs'].extend([entry.to_dict() for entry in entries_to_write])
            
            with open(self.current_json_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.debug("Flushed %d log entries to disk", len(entries_to_write))
            
        except Exception as e:
            logger.error("Failed to flush buffer: %s", e)
    
    def get_statistics(self, mode: Optional[str] = None) -> Dict:
        """Get logging statistics"""
        if mode:
            return {
                'mode': mode,
                'total_transmissions': self.stats[mode]['total'],
                'unique_sources': len(self.stats[mode]['sources']),
                'unique_talkgroups': len(self.stats[mode]['talkgroups']),
                'recent_sources': list(self.stats[mode]['sources'])[-20:],
                'recent_talkgroups': list(self.stats[mode]['talkgroups'])[-20:]
            }
        else:
            return {
                'all_modes': {
                    mode: {
                        'total': stats['total'],
                        'unique_sources': len(stats['sources']),
                        'unique_talkgroups': len(stats['talkgroups'])
                    }
                    for mode, stats in self.stats.items()
                },
                'buffer_size': len(self.buffer),
                'current_csv': self.current_csv_file,
                'current_json': self.current_json_file
            }
    
    def search_logs(self, query: Dict, days_back: int = 7) -> List[Dict]:
        """Search through log files
        
        Args:
            query: Dict with search criteria (source, destination, mode, etc.)
            days_back: Number of days to search back
            
        Returns:
            List of matching log entries
        """
        results = []
        
        for day_offset in range(days_back):
            date = datetime.now() - timedelta(days=day_offset)
            date_str = date.strftime('%Y%m%d')
            csv_file = os.path.join(self.data_dir, f'digital_voice_{date_str}.csv')
            
            if not os.path.exists(csv_file):
                continue
            
            try:
                with open(csv_file, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        matches = True
                        
                        for key, value in query.items():
                            if key in row and str(row[key]).lower() != str(value).lower():
                                matches = False
                                break
                        
                        if matches:
                            results.append(row)
            
            except Exception as e:
                logger.error("Error searching log file %s: %s", csv_file, e)
        
        return results
    
    def cleanup_old_logs(self, days_to_keep: int = 30):
        """Delete log files older than specified days"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        deleted_count = 0
        
        for filename in os.listdir(self.data_dir):
            if not (filename.startswith('digital_voice_') and 
                    (filename.endswith('.csv') or filename.endswith('.json'))):
                continue
            
            filepath = os.path.join(self.data_dir, filename)
            
            try:
                file_time = datetime.fromtimestamp(os.path.getctime(filepath))
                
                if file_time < cutoff_date:
                    os.remove(filepath)
                    deleted_count += 1
                    logger.debug("Deleted old log file: %s", filename)
            
            except Exception as e:
                logger.error("Error processing log file %s: %s", filename, e)
        
        if deleted_count > 0:
            logger.info("🗑️  Cleanup: deleted %d log files older than %d days", 
                       deleted_count, days_to_keep)


# Initialize statistics with default dict of sets
def _init_stats():
    stats = defaultdict(lambda: {'total': 0, 'sources': set(), 'talkgroups': set()})
    return stats

DigitalVoiceLogger.stats = _init_stats()


def init_digital_voice_logger():
    """Initialize and start the digital voice logger"""
    try:
        logger_instance = DigitalVoiceLogger.get_instance()
        logger_instance.start()
        
        # Start cleanup thread
        def cleanup_worker():
            while True:
                time.sleep(86400)  # Run once per day
                logger_instance.cleanup_old_logs(30)
        
        threading.Thread(target=cleanup_worker, daemon=True).start()
        
    except Exception as e:
        logger.error("Failed to initialize DigitalVoiceLogger: %s", e)


if __name__ == "__main__":
    # Test the logger
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    logger_inst = DigitalVoiceLogger.get_instance()
    logger_inst.start()
    
    print("\n📝 Digital Voice Logger Test")
    print("=" * 50)
    print(f"CSV log: {logger_inst.current_csv_file}")
    print(f"JSON log: {logger_inst.current_json_file}")
    
    # Simulate some transmissions
    print("\nSimulating transmissions...")
    
    logger_inst.log_transmission('DMR', {
        'frequency': 438450000,
        'source': 'IU2VTX',
        'source_id': 2227001,
        'talkgroup_id': 2227,
        'slot': 2,
        'color_code': 1,
        'rssi': -85,
        'duration': 5.2
    })
    
    logger_inst.log_transmission('YSF', {
        'frequency': 438450000,
        'source': 'IU2VTX',
        'destination': 'ALL',
        'rssi': -80
    })
    
    logger_inst.log_transmission('DMR', {
        'frequency': 438450000,
        'source': 'IW2QMO',
        'source_id': 2227002,
        'talkgroup_id': 9,
        'slot': 1,
        'color_code': 1,
        'rssi': -90
    })
    
    time.sleep(1)
    logger_inst._flush_buffer()
    
    print("✅ Test transmissions logged")
    print("\nStatistics:")
    stats = logger_inst.get_statistics()
    print(json.dumps(stats, indent=2, default=str))
    
    print("\nSearch test (DMR):")
    results = logger_inst.search_logs({'Mode': 'DMR'})
    print(f"Found {len(results)} DMR transmissions")
    
    logger_inst.stop()
    print("\n✅ Test completed!")
