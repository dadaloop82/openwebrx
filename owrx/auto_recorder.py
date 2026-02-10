"""
Automatic continuous audio recording system
Records audio continuously, creates new files on frequency change,
and automatically cleans up old recordings after 7 days
FEATURE: ID3 metadata injection for all recordings
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

# Try to import mutagen for ID3 tags, fallback to eyeD3 if not available
try:
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, COMM, TXXX
    METADATA_LIB = 'mutagen'
except ImportError:
    try:
        import eyed3
        METADATA_LIB = 'eyed3'
    except ImportError:
        METADATA_LIB = None
        logger.warning("⚠️  No metadata library found. Install mutagen or eyed3 for ID3 tag support")


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
        self.current_mode = "Unknown"
        self.current_process = None
        self.current_filename = None
        self.monitoring_thread = None
        self.cleanup_thread = None
        self.running = False
        self.last_frequency_check = 0
        self.receiver_info = self._get_receiver_info()
        
        # Assicura che la directory esista
        os.makedirs(self.recording_dir, exist_ok=True)
        
        logger.info("AutoRecorder initialized - recordings dir: %s", self.recording_dir)
        if METADATA_LIB:
            logger.info("🏷️  Metadata support enabled using: %s", METADATA_LIB)
    
    def _get_receiver_info(self):
        """Get receiver information for metadata"""
        try:
            from owrx.config import Config
            pm = Config.get()
            return {
                'name': pm.get('receiver_name', 'OpenWebRX'),
                'location': pm.get('receiver_location', 'Unknown'),
                'admin': pm.get('receiver_admin', 'Unknown')
            }
        except:
            return {
                'name': 'OpenWebRX',
                'location': 'Unknown',
                'admin': 'Unknown'
            }
    
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
        logger.info("   Metadata injection: %s", "✅ ENABLED" if METADATA_LIB else "❌ DISABLED")
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
    
    def _inject_metadata(self, filepath, frequency, mode, start_time):
        """Inject ID3 metadata tags into MP3 file"""
        if not METADATA_LIB:
            return
        
        try:
            freq_mhz = frequency / 1_000_000
            
            if METADATA_LIB == 'mutagen':
                audio = MP3(filepath, ID3=ID3)
                
                # Add ID3 tag if it doesn't exist
                try:
                    audio.add_tags()
                except:
                    pass
                
                # Title: frequency and timestamp
                audio.tags.add(TIT2(encoding=3, text=f"{freq_mhz:.3f} MHz - {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"))
                
                # Artist: receiver name
                audio.tags.add(TPE1(encoding=3, text=self.receiver_info['name']))
                
                # Album: location and mode
                audio.tags.add(TALB(encoding=3, text=f"{self.receiver_info['location']} - {mode}"))
                
                # Year: recording year
                audio.tags.add(TDRC(encoding=3, text=start_time.strftime('%Y')))
                
                # Comment: detailed info
                comment_text = (
                    f"Frequency: {freq_mhz:.6f} MHz\n"
                    f"Mode: {mode}\n"
                    f"Receiver: {self.receiver_info['name']}\n"
                    f"Location: {self.receiver_info['location']}\n"
                    f"Recorded: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                    f"Operator: {self.receiver_info['admin']}"
                )
                audio.tags.add(COMM(encoding=3, lang='eng', desc='Recording Info', text=comment_text))
                
                # Custom tags for exact frequency
                audio.tags.add(TXXX(encoding=3, desc='Frequency_Hz', text=str(frequency)))
                audio.tags.add(TXXX(encoding=3, desc='Mode', text=mode))
                audio.tags.add(TXXX(encoding=3, desc='Timestamp_UTC', text=start_time.isoformat()))
                
                audio.save()
                logger.info("🏷️  Metadata injected: %.3f MHz, %s, %s", 
                          freq_mhz, mode, start_time.strftime('%Y-%m-%d %H:%M:%S'))
            
            elif METADATA_LIB == 'eyed3':
                audiofile = eyed3.load(filepath)
                if audiofile.tag is None:
                    audiofile.initTag()
                
                audiofile.tag.title = f"{freq_mhz:.3f} MHz - {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                audiofile.tag.artist = self.receiver_info['name']
                audiofile.tag.album = f"{self.receiver_info['location']} - {mode}"
                audiofile.tag.recording_date = start_time.year
                
                comment_text = (
                    f"Frequency: {freq_mhz:.6f} MHz | "
                    f"Mode: {mode} | "
                    f"Receiver: {self.receiver_info['name']} | "
                    f"Location: {self.receiver_info['location']} | "
                    f"Recorded: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                )
                audiofile.tag.comments.set(comment_text)
                
                audiofile.tag.save()
                logger.info("🏷️  Metadata injected: %.3f MHz, %s, %s", 
                          freq_mhz, mode, start_time.strftime('%Y-%m-%d %H:%M:%S'))
        
        except Exception as e:
            logger.error("Failed to inject metadata: %s", e)
    
    def _start_new_recording(self, frequency, mode="Unknown"):
        """Start a new recording with frequency-based filename"""
        # Ferma registrazione precedente
        self._stop_current_recording()
        
        # Crea nuovo filename: FREQ_YYYYMMDD_HHMMSS.mp3
        start_time = datetime.utcnow()
        timestamp = start_time.strftime('%Y%m%d_%H%M%S')
        freq_mhz = frequency / 1_000_000
        filename = f"{freq_mhz:.3f}MHz_{timestamp}.mp3"
        filepath = os.path.join(self.recording_dir, filename)
        
        self.current_mode = mode
        
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
            logger.info("📼 Recording started: %s (freq: %.3f MHz, mode: %s)", 
                       filename, freq_mhz, mode)
            
            # Inject metadata after a short delay to ensure file is created
            if METADATA_LIB:
                threading.Timer(2.0, self._inject_metadata, 
                              args=[filepath, frequency, mode, start_time]).start()
            
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
