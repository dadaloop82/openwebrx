"""
Automatic Audio Recorder with Squelch Detection
Records MP3 files when audio present, auto-closes on silence
"""

import os
import time
import threading
import wave
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from owrx.recording_notifier import get_notifier

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
MIN_DURATION_SECONDS = 5
MAX_AGE_DAYS = 7
CLEANUP_INTERVAL = 300
SILENCE_TIMEOUT = 3.0

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
        self.current_wav_path = None
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
            filename = f"{freq_mhz:.4f}MHz_{timestamp}.mp3"
        else:
            filename = f"REC_{timestamp}.mp3"
        
        self.current_filepath = self.recordings_dir / filename
        self.current_wav_path = self.recordings_dir / f"temp_{timestamp}.wav"
        self.current_frequency_hz = frequency_hz
        self.recording_start_time = time.time()
        self.last_audio_time = time.time()
        self.is_recording = True
        
        # Create temporary WAV file
        self.current_wavfile = wave.open(str(self.current_wav_path), 'wb')
        self.current_wavfile.setnchannels(1)
        self.current_wavfile.setsampwidth(2)
        self.current_wavfile.setframerate(12000)
        
        logger.info("📼 Recording started: %s", filename)
        
        try:
            notifier = get_notifier()
            notifier.notify_recording_start(self.current_frequency_hz)
        except:
            pass
    
    def _stop_recording(self):
        """Stop current recording and convert to MP3"""
        if not self.is_recording:
            return
            
        duration = time.time() - self.recording_start_time
        filepath = self.current_filepath
        wav_path = self.current_wav_path
        
        # Close WAV file
        try:
            if self.current_wavfile:
                self.current_wavfile.close()
                self.current_wavfile = None
        except Exception as e:
            logger.error("Error closing WAV file: %s", e)
        
        self.is_recording = False
        self.current_filepath = None
        self.current_wav_path = None
        
        try:
            notifier = get_notifier()
            notifier.notify_recording_stop()
        except:
            pass
        
        logger.info("📼 Recording stopped: %s (%.1fs)", filepath.name if filepath else "?", duration)
        
        # Check duration
        if duration < MIN_DURATION_SECONDS:
            try:
                if wav_path and wav_path.exists():
                    wav_path.unlink()
                    logger.info("🗑️  Deleted short recording (%.1fs < %ds)", duration, MIN_DURATION_SECONDS)
            except Exception as e:
                logger.error("Error deleting short recording: %s", e)
        else:
            # Convert WAV to MP3
            self._convert_to_mp3(wav_path, filepath)
    
    def _convert_to_mp3(self, wav_path: Path, mp3_path: Path):
        """Convert WAV to MP3 using ffmpeg"""
        try:
            if not wav_path.exists():
                logger.error("WAV file not found: %s", wav_path)
                return
            
            # Use ffmpeg to convert with good quality
            cmd = [
                'ffmpeg', '-y', '-i', str(wav_path),
                '-codec:a', 'libmp3lame', '-qscale:a', '2',
                str(mp3_path)
            ]
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60
            )
            
            if result.returncode == 0 and mp3_path.exists():
                logger.info("✅ Converted to MP3: %s", mp3_path.name)
                # Delete temporary WAV
                wav_path.unlink()
            else:
                logger.error("ffmpeg conversion failed (code %d)", result.returncode)
                # Keep WAV if conversion failed
                
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout converting %s", wav_path)
        except Exception as e:
            logger.error("Error converting to MP3: %s", e)
    
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
                self._stop_recording()
    
    def write_audio_chunk(self, audio_data: bytes, frequency_hz: Optional[int] = None):
        """Write audio chunk - starts/continues recording"""
        if len(audio_data) == 0:
            return
        
        with self._lock:
            if not self.is_recording:
                self._start_recording(frequency_hz)
            
            self.last_audio_time = time.time()
            
            try:
                if self.current_wavfile:
                    self.current_wavfile.writeframes(audio_data)
            except Exception as e:
                logger.error("Error writing audio chunk: %s", e)
            
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
                
                for filepath in self.recordings_dir.glob("*.mp3"):
                    try:
                        if filepath.stat().st_mtime < cutoff_time:
                            filepath.unlink()
                            deleted_count += 1
                    except Exception as e:
                        logger.error("Error deleting %s: %s", filepath.name, e)
                
                # Cleanup temp WAV files
                for filepath in self.recordings_dir.glob("temp_*.wav"):
                    try:
                        if filepath.stat().st_mtime < time.time() - 3600:  # Older than 1 hour
                            filepath.unlink()
                            deleted_count += 1
                    except Exception as e:
                        logger.error("Error deleting temp %s: %s", filepath.name, e)
                
                if deleted_count > 0:
                    logger.info("🧹 Cleanup: deleted %d old files", deleted_count)
                    
            except Exception as e:
                logger.error("Cleanup worker error: %s", e)


_recorder_instance = None


def get_recorder() -> SquelchRecorder:
    """Get singleton recorder instance"""
    global _recorder_instance
    if _recorder_instance is None:
        _recorder_instance = SquelchRecorder()
    return _recorder_instance
