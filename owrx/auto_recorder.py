"""
Automatic continuous audio recording system
Records audio continuously, creates new files on frequency change,
and automatically cleans up old recordings after 7 days
"""

import os
import time
import logging
import threading
import subprocess
from datetime import datetime, timedelta
from owrx.config.core import CoreConfig
from owrx.storage import Storage

logger = logging.getLogger(__name__)


class AutoRecorder:
    """Manages continuous audio recording with automatic file rotation"""
    
    instance = None
    lock = threading.Lock()
    
    @staticmethod
    def get_instance():
        with AutoRecorder.lock:
            if AutoRecorder.instance is None:
                AutoRecorder.instance = AutoRecorder()
        return AutoRecorder.instance
    
    def __init__(self):
        self.recording_dir = CoreConfig().get_temporary_directory()
        self.current_frequency = None
        self.current_process = None
        self.current_filename = None
        self.monitoring_thread = None
        self.cleanup_thread = None
        self.running = False
        self.last_frequency_check = 0
        
        # Assicura che la directory esista
        os.makedirs(self.recording_dir, exist_ok=True)
        
        logger.info("AutoRecorder initialized - recordings dir: %s", self.recording_dir)
    
    def start(self):
        """Start continuous recording and monitoring"""
        if self.running:
            logger.warning("AutoRecorder already running")
            return
        
        self.running = True
        
        # Avvia thread di monitoraggio frequenza
        self.monitoring_thread = threading.Thread(target=self._monitor_frequency, daemon=True)
        self.monitoring_thread.start()
        
        # Avvia thread di pulizia file vecchi
        self.cleanup_thread = threading.Thread(target=self._cleanup_old_files, daemon=True)
        self.cleanup_thread.start()
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("🎙️  AUTO RECORDER STARTED")
        logger.info("   Continuous recording enabled")
        logger.info("   Auto-cleanup: 7 days")
        logger.info("   Files in: %s", self.recording_dir)
        logger.info("═══════════════════════════════════════════════════")
    
    def stop(self):
        """Stop continuous recording"""
        self.running = False
        self._stop_current_recording()
        
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=5)
        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=5)
        
        logger.info("AutoRecorder stopped")
    
    def _monitor_frequency(self):
        """Monitor frequency changes and manage recording"""
        while self.running:
            try:
                # Qui dovresti ottenere la frequenza corrente dal receiver
                # Per ora usiamo un placeholder - dovrai integrarlo con il tuo sistema
                current_freq = self._get_current_frequency()
                
                if current_freq and current_freq != self.current_frequency:
                    logger.info("Frequency changed: %d Hz -> %d Hz", 
                              self.current_frequency or 0, current_freq)
                    self._start_new_recording(current_freq)
                    self.current_frequency = current_freq
                
                time.sleep(2)  # Controlla ogni 2 secondi
                
            except Exception as e:
                logger.error("Error in frequency monitoring: %s", e)
                time.sleep(5)
    
    def _get_current_frequency(self):
        """
        Get current receiver frequency
        TODO: Implement actual frequency detection from OpenWebRX receiver
        For now returns None - needs integration with receiver state
        """
        # Placeholder - dovrai implementare la logica per ottenere la frequenza corrente
        # dal receiver attivo in OpenWebRX
        return None
    
    def _start_new_recording(self, frequency):
        """Start a new recording with frequency-based filename"""
        # Ferma registrazione precedente
        self._stop_current_recording()
        
        # Crea nuovo filename: FREQ_YYYYMMDD_HHMMSS.mp3
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        freq_mhz = frequency / 1_000_000
        filename = f"{freq_mhz:.3f}MHz_{timestamp}.mp3"
        filepath = os.path.join(self.recording_dir, filename)
        
        # Avvia nuova registrazione
        # Nota: questo è un esempio usando ffmpeg - potrebbe dover essere adattato
        # per l'audio pipeline di OpenWebRX
        try:
            cmd = [
                'ffmpeg',
                '-f', 'pulse',  # o 'alsa' a seconda del sistema
                '-i', 'default',
                '-acodec', 'libmp3lame',
                '-b:a', '128k',
                '-y',
                filepath
            ]
            
            self.current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            self.current_filename = filename
            logger.info("📼 Recording started: %s (freq: %.3f MHz)", filename, freq_mhz)
            
        except Exception as e:
            logger.error("Failed to start recording: %s", e)
    
    def _stop_current_recording(self):
        """Stop current recording process"""
        if self.current_process:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=5)
                logger.info("⏹️  Recording stopped: %s", self.current_filename)
            except Exception as e:
                logger.error("Error stopping recording: %s", e)
                try:
                    self.current_process.kill()
                except:
                    pass
            
            self.current_process = None
            self.current_filename = None
    
    def _cleanup_old_files(self):
        """Delete recordings older than 7 days"""
        while self.running:
            try:
                now = datetime.now()
                cutoff = now - timedelta(days=7)
                deleted_count = 0
                
                for filename in os.listdir(self.recording_dir):
                    if not filename.endswith('.mp3'):
                        continue
                    
                    filepath = os.path.join(self.recording_dir, filename)
                    
                    try:
                        file_time = datetime.fromtimestamp(os.path.getctime(filepath))
                        
                        if file_time < cutoff:
                            os.remove(filepath)
                            deleted_count += 1
                            logger.debug("Deleted old recording: %s", filename)
                    
                    except Exception as e:
                        logger.error("Error processing file %s: %s", filename, e)
                
                if deleted_count > 0:
                    logger.info("🗑️  Cleanup: deleted %d recordings older than 7 days", deleted_count)
                
                # Esegui pulizia ogni ora
                time.sleep(3600)
                
            except Exception as e:
                logger.error("Error in cleanup thread: %s", e)
                time.sleep(3600)


# Funzione per inizializzare l'auto-recorder all'avvio di OpenWebRX
def init_auto_recorder():
    """Initialize and start the auto recorder"""
    try:
        recorder = AutoRecorder.get_instance()
        recorder.start()
    except Exception as e:
        logger.error("Failed to initialize AutoRecorder: %s", e)
