"""
Automatic Audio Recorder with Squelch Detection
Integrated with OpenWebRX DSP chain
"""

import os
import time
import threading
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
MIN_DURATION_SECONDS = 5
MAX_AGE_DAYS = 7
CLEANUP_INTERVAL = 300  # 5 minutes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SquelchRecorder:
    """Audio recorder triggered by squelch state"""
    
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
            
        self.recordings_dir = Path(RECORDINGS_DIR)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        
        self.current_recording = None
        self.recording_start_time = None
        self.current_filepath = None
        self.current_frequency_hz = None
        self.is_recording = False
        
        self.cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True)
        self.cleanup_thread.start()
        
        self._initialized = True
        
        logger.info("🎙️  Squelch Recorder initialized - directory: %s", self.recordings_dir)
    
    def on_squelch_open(self, frequency_hz: Optional[int] = None):
        """Called when squelch opens (signal detected)"""
        if self.is_recording:
            return
            
        with self._lock:
            timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            
            if frequency_hz:
                freq_mhz = frequency_hz / 1_000_000
                filename = f"{freq_mhz:.4f}MHz_{timestamp}.mp3"
            else:
                filename = f"REC_{timestamp}.mp3"
            
            self.current_filepath = self.recordings_dir / filename
            self.current_frequency_hz = frequency_hz
            self.recording_start_time = time.time()
            self.is_recording = True
            
            logger.info("📼 Recording started: %s", filename)
    
    def on_squelch_close(self):
        """Called when squelch closes (signal lost)"""
        if not self.is_recording:
            return
            
        with self._lock:
            duration = time.time() - self.recording_start_time
            filepath = self.current_filepath
            
            self.is_recording = False
            self.current_filepath = None
            self.current_frequency_hz = None
            self.recording_start_time = None
            
            # Check duration after short delay to ensure file is written
            threading.Timer(1.0, self._check_recording_duration, args=[filepath, duration]).start()
    
    def _check_recording_duration(self, filepath: Path, expected_duration: float):
        """Check if recording should be kept or deleted"""
        try:
            if not filepath.exists():
                logger.warning("Recording file not found: %s", filepath.name)
                return
            
            # Get actual file size
            file_size = filepath.stat().st_size
            
            # If file too small or duration too short, delete it
            if expected_duration < MIN_DURATION_SECONDS or file_size < 10000:  # < 10KB
                filepath.unlink()
                logger.info("🗑️  Deleted short recording (%.1fs, %d bytes): %s", 
                          expected_duration, file_size, filepath.name)
            else:
                logger.info("✅ Recording saved (%.1fs, %.2f KB): %s", 
                          expected_duration, file_size/1024, filepath.name)
                
        except Exception as e:
            logger.error("Error checking recording: %s", e)
    
    def write_audio_chunk(self, audio_data: bytes):
        """Write audio chunk to current recording"""
        if not self.is_recording or not self.current_filepath:
            return
            
        try:
            # Append audio data to file
            with open(self.current_filepath, 'ab') as f:
                f.write(audio_data)
        except Exception as e:
            logger.error("Error writing audio chunk: %s", e)
    
    def get_status(self) -> dict:
        """Get current recording status"""
        if self.is_recording:
            duration = time.time() - self.recording_start_time
            return {
                'recording': True,
                'duration': duration,
                'file': self.current_filepath.name if self.current_filepath else None,
                'frequency_hz': self.current_frequency_hz
            }
        return {'recording': False}
    
    def _cleanup_worker(self):
        """Background thread for cleaning old recordings"""
        while True:
            try:
                time.sleep(CLEANUP_INTERVAL)
                self._cleanup_old_files()
            except Exception as e:
                logger.error("Cleanup error: %s", e)
    
    def _cleanup_old_files(self):
        """Delete recordings older than MAX_AGE_DAYS"""
        try:
            cutoff_time = time.time() - (MAX_AGE_DAYS * 86400)
            deleted = 0
            
            for file in self.recordings_dir.glob("*.mp3"):
                try:
                    if file.stat().st_mtime < cutoff_time:
                        file.unlink()
                        deleted += 1
                except:
                    pass
            
            if deleted > 0:
                logger.info("🗑️  Cleaned up %d old recordings", deleted)
                
        except Exception as e:
            logger.error("Cleanup error: %s", e)


# Global instance
_recorder = None

def get_recorder() -> SquelchRecorder:
    """Get global recorder instance"""
    global _recorder
    if _recorder is None:
        _recorder = SquelchRecorder()
    return _recorder
