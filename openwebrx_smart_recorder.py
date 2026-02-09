#!/usr/bin/env python3
"""
Smart Audio Recorder with Squelch Detection
Records only when signal is present, deletes files < 5 seconds
"""

import os
import time
import subprocess
import logging
from datetime import datetime, timedelta
from pathlib import Path
import threading

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
MIN_DURATION_SECONDS = 5
MAX_AGE_DAYS = 7
SQUELCH_CHECK_INTERVAL = 0.5  # Check squelch every 0.5 seconds
CLEANUP_INTERVAL = 300  # Cleanup every 5 minutes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SmartRecorder:
    """Intelligent recorder with squelch detection"""
    
    def __init__(self):
        self.recordings_dir = Path(RECORDINGS_DIR)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.current_recording = None
        self.recording_start_time = None
        self.current_filename = None
        self.is_recording = False
        self.running = True
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("🎙️  SMART AUDIO RECORDER")
        logger.info("   Squelch-triggered recording")
        logger.info("   Min duration: %d seconds", MIN_DURATION_SECONDS)
        logger.info("   Auto-cleanup: %d days", MAX_AGE_DAYS)
        logger.info("   Directory: %s", self.recordings_dir)
        logger.info("═══════════════════════════════════════════════════")
    
    def check_squelch(self):
        """
        Check if squelch is open (signal present)
        TODO: Integrate with OpenWebRX squelch state
        For now, returns False (no recording)
        """
        # Placeholder - needs integration with OpenWebRX receiver state
        # Should check if audio level > squelch threshold
        return False
    
    def start_recording(self, frequency_hz=None):
        """Start a new recording"""
        if self.is_recording:
            return
        
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        
        if frequency_hz:
            freq_mhz = frequency_hz / 1_000_000
            filename = f"{freq_mhz:.3f}MHz_{timestamp}.mp3"
        else:
            filename = f"REC_{timestamp}.mp3"
        
        filepath = self.recordings_dir / filename
        
        try:
            # Start recording with ffmpeg
            # This is a placeholder - needs integration with OpenWebRX audio pipeline
            cmd = [
                'ffmpeg',
                '-f', 'pulse',
                '-i', 'default',
                '-acodec', 'libmp3lame',
                '-b:a', '128k',
                '-y',
                str(filepath)
            ]
            
            self.current_recording = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            self.current_filename = filename
            self.recording_start_time = time.time()
            self.is_recording = True
            
            logger.info("📼 Recording started: %s", filename)
            
        except Exception as e:
            logger.error("Failed to start recording: %s", e)
    
    def stop_recording(self):
        """Stop current recording and check duration"""
        if not self.is_recording:
            return
        
        try:
            self.current_recording.terminate()
            self.current_recording.wait(timeout=5)
            
            duration = time.time() - self.recording_start_time
            filepath = self.recordings_dir / self.current_filename
            
            if duration < MIN_DURATION_SECONDS:
                # Delete short recordings
                if filepath.exists():
                    filepath.unlink()
                    logger.info("🗑️  Deleted short recording (%.1fs): %s", 
                              duration, self.current_filename)
            else:
                logger.info("⏹️  Recording saved (%.1fs): %s", 
                          duration, self.current_filename)
            
        except Exception as e:
            logger.error("Error stopping recording: %s", e)
            try:
                self.current_recording.kill()
            except:
                pass
        
        finally:
            self.current_recording = None
            self.current_filename = None
            self.recording_start_time = None
            self.is_recording = False
    
    def cleanup_old_files(self):
        """Delete recordings older than MAX_AGE_DAYS"""
        try:
            cutoff_time = time.time() - (MAX_AGE_DAYS * 86400)
            deleted = 0
            
            for file in self.recordings_dir.glob("*.mp3"):
                if file.stat().st_mtime < cutoff_time:
                    file.unlink()
                    deleted += 1
                    logger.debug("Deleted old recording: %s", file.name)
            
            if deleted > 0:
                logger.info("🗑️  Cleaned up %d old recordings", deleted)
                
        except Exception as e:
            logger.error("Error during cleanup: %s", e)
    
    def run(self):
        """Main loop with squelch monitoring"""
        logger.info("Smart recorder started")
        
        last_cleanup = time.time()
        squelch_was_open = False
        
        while self.running:
            try:
                # Check squelch state
                squelch_open = self.check_squelch()
                
                if squelch_open and not squelch_was_open:
                    # Squelch just opened - start recording
                    self.start_recording()
                elif not squelch_open and squelch_was_open:
                    # Squelch just closed - stop recording
                    self.stop_recording()
                
                squelch_was_open = squelch_open
                
                # Periodic cleanup
                if time.time() - last_cleanup > CLEANUP_INTERVAL:
                    self.cleanup_old_files()
                    last_cleanup = time.time()
                
                time.sleep(SQUELCH_CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error("Error in main loop: %s", e)
                time.sleep(1)
        
        # Stop any active recording
        if self.is_recording:
            self.stop_recording()


if __name__ == '__main__':
    recorder = SmartRecorder()
    recorder.run()
