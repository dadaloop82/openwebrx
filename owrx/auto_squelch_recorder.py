"""
Automatic Audio Recorder with Squelch Detection
Records WAV files when audio present, auto-closes on silence
"""

import os
import time
import threading
import wave
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from owrx.recording_notifier import get_notifier

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
MIN_DURATION_SECONDS = 5
MAX_AGE_DAYS = 7
CLEANUP_INTERVAL = 300  # 5 minutes
SILENCE_TIMEOUT = 3.0  # Close recording after 3 seconds of silence

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SquelchRecorder:
    """Audio recorder triggered by audio presence"""
    
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
        
        self.current_wavfile = None
        self.recording_start_time = None
        self.current_filepath = None
        self.current_frequency_hz = None
        self.is_recording = False
        self.last_audio_time = None
        self.silence_timer = None
        
        self.cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True)
        self.cleanup_thread.start()
        
        self._initialized = True
        
        logger.info("🎙️  Squelch Recorder initialized - directory: %s", self.recordings_dir)
    
    def _start_recording(self, frequency_hz: Optional[int] = None):
        """Start a new recording"""
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        
        if frequency_hz:
            freq_mhz = frequency_hz / 1_000_000
            filename = f"{freq_mhz:.4f}MHz_{timestamp}.wav"
        else:
            filename = f"REC_{timestamp}.wav"
        
        self.current_filepath = self.recordings_dir / filename
        self.current_frequency_hz = frequency_hz
        self.recording_start_time = time.time()
        self.last_audio_time = time.time()
        self.is_recording = True
        
        # Create WAV file
        self.current_wavfile = wave.open(str(self.current_filepath), 'wb')
        self.current_wavfile.setnchannels(1)
        self.current_wavfile.setsampwidth(2)  # 16-bit
        self.current_wavfile.setframerate(12000)  # OpenWebRX audio sample rate
        
        logger.info("📼 Recording started: %s", filename)
        
        # Notify clients
        try:
            notifier = get_notifier()
            notifier.notify_recording_start(self.current_frequency_hz)
        except:
            pass
    
    def _stop_recording(self):
        """Stop current recording and check duration"""
        if not self.is_recording:
            return
            
        duration = time.time() - self.recording_start_time
        filepath = self.current_filepath
        
        # Close WAV file
        try:
            if self.current_wavfile:
                self.current_wavfile.close()
                self.current_wavfile = None
        except Exception as e:
            logger.error("Error closing WAV file: %s", e)
        
        self.is_recording = False
        self.current_filepath = None
        
        # Notify clients
        try:
            notifier = get_notifier()
            notifier.notify_recording_stop()
        except:
            pass
        
        logger.info("📼 Recording stopped: %s (%.1fs)", filepath.name if filepath else "?", duration)
        
        # Check duration and delete if too short
        self._check_recording_duration(filepath, duration)
    
    def _check_recording_duration(self, filepath: Path, duration: float):
        """Delete recording if shorter than minimum duration"""
        if duration < MIN_DURATION_SECONDS:
            try:
                if filepath and filepath.exists():
                    filepath.unlink()
                    logger.info("🗑️  Deleted short recording: %s (%.1fs < %ds)", 
                               filepath.name, duration, MIN_DURATION_SECONDS)
            except Exception as e:
                logger.error("Error deleting short recording: %s", e)
    
    def _cancel_silence_timer(self):
        """Cancel pending silence timeout"""
        if self.silence_timer:
            self.silence_timer.cancel()
            self.silence_timer = None
    
    def _schedule_silence_timeout(self):
        """Schedule auto-close on silence"""
        self._cancel_silence_timer()
        self.silence_timer = threading.Timer(SILENCE_TIMEOUT, self._on_silence_timeout)
        self.silence_timer.start()
    
    def _on_silence_timeout(self):
        """Called when silence timeout expires"""
        with self._lock:
            current_silence_duration = time.time() - self.last_audio_time
            if current_silence_duration >= SILENCE_TIMEOUT:
                logger.debug("Silence timeout - closing recording")
                self._stop_recording()
    
    def write_audio_chunk(self, audio_data: bytes, frequency_hz: Optional[int] = None):
        """Write audio chunk - starts/continues recording"""
        if len(audio_data) == 0:
            return
        
        with self._lock:
            # Start recording if not already started
            if not self.is_recording:
                self._start_recording(frequency_hz)
            
            # Update last audio time
            self.last_audio_time = time.time()
            
            # Write audio data to WAV file
            try:
                if self.current_wavfile:
                    self.current_wavfile.writeframes(audio_data)
            except Exception as e:
                logger.error("Error writing audio chunk: %s", e)
            
            # Reset silence timeout
            self._schedule_silence_timeout()
    
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
        """Background thread to cleanup old recordings"""
        logger.info("🧹 Cleanup worker started (max age: %d days)", MAX_AGE_DAYS)
        
        while True:
            try:
                time.sleep(CLEANUP_INTERVAL)
                
                cutoff_time = time.time() - (MAX_AGE_DAYS * 86400)
                deleted_count = 0
                
                for filepath in self.recordings_dir.glob("*.wav"):
                    try:
                        if filepath.stat().st_mtime < cutoff_time:
                            filepath.unlink()
                            deleted_count += 1
                            logger.debug("Deleted old recording: %s", filepath.name)
                    except Exception as e:
                        logger.error("Error deleting %s: %s", filepath.name, e)
                
                if deleted_count > 0:
                    logger.info("🧹 Cleanup: deleted %d old recordings", deleted_count)
                    
            except Exception as e:
                logger.error("Cleanup worker error: %s", e)


_recorder_instance = None


def get_recorder() -> SquelchRecorder:
    """Get singleton recorder instance"""
    global _recorder_instance
    if _recorder_instance is None:
        _recorder_instance = SquelchRecorder()
    return _recorder_instance
