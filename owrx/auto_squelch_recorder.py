"""
Automatic Audio Recorder with Squelch Detection
Records MP3 files when audio signal detected, auto-closes on silence
Taps into the pre-compression audio buffer (PCM FLOAT32) from the DSP chain
"""

import os
import time
import threading
import wave
import struct
import subprocess
import logging
import array
from datetime import datetime
from pathlib import Path
from typing import Optional

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
MIN_DURATION_SECONDS = 5
MAX_RECORDING_DURATION = 300  # Max 5 minutes per recording (safety net for digital modes)
MAX_AGE_DAYS = 7
CLEANUP_INTERVAL = 300
SILENCE_TIMEOUT = 3.0
FREQ_DWELL_SECONDS = 2.0  # Must stay on a frequency this long before recording starts
AUDIO_RMS_THRESHOLD = 0.010  # Float threshold (range -1.0 to 1.0) - lowered for weak HF

# Digital modes where squelch recording makes no sense (decoded as text, not voice)
DIGITAL_MODES = frozenset({
    'ft8', 'ft4', 'wspr', 'jt65', 'jt9', 'js8',
    'msk144', 'q65', 'fst4', 'fst4w',
    'packet', 'aprs', 'pocsag', 'adsb', 'acars',
    'rtty', 'psk', 'psk31', 'psk63',
    'dmr', 'dstar', 'nxdn', 'ysf', 'm17', 'freedv',
})

logger = logging.getLogger(__name__)


class SquelchRecorder:
    """Audio recorder triggered by audio signal level detection"""
    
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
        self.last_signal_time = None
        self.silence_timer = None
        self.chunk_count = 0
        self._rec_lock = threading.Lock()  # Instance-level lock for recording state
        
        # Frequency change tracking
        self._last_seen_freq = None
        self._freq_stable_since = None  # When the current frequency was first seen
        self._current_mode = None  # Set by external caller (e.g. wav.py or orchestrator)
        self._recording_disabled_for_mode = False
        self._station_name = None  # Set by orchestrator for event-aware recording titles
        
        self.cleanup_thread = threading.Thread(target=self._cleanup_worker, daemon=True)
        self.cleanup_thread.start()
        
        self._status_thread = threading.Thread(target=self._status_broadcaster, daemon=True)
        self._status_thread.start()
        
        # Clean orphan temp WAV files from previous runs
        try:
            import glob
            for f in glob.glob(os.path.join(self.recordings_dir, "temp_*.wav")):
                os.remove(f)
                logger.info("Cleaned orphan temp file: %s", os.path.basename(f))
        except Exception as e:
            logger.warning("Error cleaning temp files: %s", e)
        
        self._initialized = True
        
        logger.info("Squelch Recorder initialized - directory: %s (RMS threshold: %.4f, min duration: %ds, dwell: %.1fs)", 
                     self.recordings_dir, AUDIO_RMS_THRESHOLD, MIN_DURATION_SECONDS, FREQ_DWELL_SECONDS)
    
    def _float32_to_int16(self, float_bytes: bytes) -> bytes:
        """Convert raw FLOAT32 PCM bytes to INT16 PCM bytes"""
        # Each float32 sample = 4 bytes, each int16 sample = 2 bytes
        num_floats = len(float_bytes) // 4
        if num_floats == 0:
            return b''
        # Unpack as float32
        floats = struct.unpack('<%df' % num_floats, float_bytes[:num_floats * 4])
        # Convert to int16 with clipping
        int16_samples = []
        for f in floats:
            # Clamp to -1.0 .. 1.0 then scale to int16 range
            clamped = max(-1.0, min(1.0, f))
            int16_samples.append(int(clamped * 32767))
        # Pack as int16
        return struct.pack('<%dh' % len(int16_samples), *int16_samples)
    
    def _compute_rms_float(self, float_bytes: bytes) -> float:
        """Compute RMS level of FLOAT32 PCM audio data (values in -1.0 to 1.0 range)"""
        try:
            num_floats = len(float_bytes) // 4
            if num_floats == 0:
                return 0.0
            floats = struct.unpack('<%df' % num_floats, float_bytes[:num_floats * 4])
            sum_sq = sum(f * f for f in floats)
            return (sum_sq / num_floats) ** 0.5
        except Exception:
            return 0.0

    def _compute_rms_int16(self, int16_bytes: bytes) -> float:
        """Compute RMS level of INT16 PCM audio data, normalized to 0.0-1.0 range"""
        try:
            num_samples = len(int16_bytes) // 2
            if num_samples == 0:
                return 0.0
            samples = struct.unpack('<%dh' % num_samples, int16_bytes[:num_samples * 2])
            sum_sq = sum(s * s for s in samples)
            rms_int16 = (sum_sq / num_samples) ** 0.5
            return rms_int16 / 32768.0  # Normalize to 0.0-1.0 range
        except Exception:
            return 0.0
    
    def _start_recording(self, frequency_hz: Optional[int] = None):
        """Start a new recording"""
        now_local = datetime.now()
        now_utc = datetime.utcnow()
        timestamp_local = now_local.strftime('%Y%m%d_%H%M%S')
        self._recording_utc_str = now_utc.strftime('%H:%M:%S UTC')
        
        # Build filename with station name if available
        parts = []
        if self._station_name:
            # Sanitize station name for filename
            safe_name = self._station_name.replace('/', '-').replace('\\', '-')
            safe_name = ''.join(c for c in safe_name if c.isalnum() or c in ' ._-()').strip()
            safe_name = safe_name[:50]  # limit length
            if safe_name:
                parts.append(safe_name)
        if frequency_hz:
            freq_mhz = frequency_hz / 1_000_000
            parts.append(f"{freq_mhz:.4f}MHz")
        parts.append(timestamp_local)
        filename = '_'.join(parts) + '.mp3' if parts else f"REC_{timestamp_local}.mp3"
        
        self.current_filepath = self.recordings_dir / filename
        self.current_wav_path = self.recordings_dir / f"temp_{timestamp_local}.wav"
        self.current_frequency_hz = frequency_hz
        self._recording_station_name = self._station_name  # snapshot for MP3 metadata
        self.recording_start_time = time.time()
        self.last_signal_time = time.time()
        self.is_recording = True
        self.chunk_count = 0
        
        # Create temporary WAV file (12000 Hz, mono, 16-bit)
        self.current_wavfile = wave.open(str(self.current_wav_path), 'wb')
        self.current_wavfile.setnchannels(1)
        self.current_wavfile.setsampwidth(2)  # 16-bit int16
        self.current_wavfile.setframerate(12000)
        
        logger.info("Recording started: %s (freq: %s)", 
                     filename, f"{frequency_hz/1e6:.4f} MHz" if frequency_hz else "unknown")
    
    def _stop_recording(self):
        """Stop current recording and convert to MP3"""
        if not self.is_recording:
            return
            
        self._cancel_silence_timer()
        
        duration = time.time() - self.recording_start_time
        filepath = self.current_filepath
        wav_path = self.current_wav_path
        
        try:
            if self.current_wavfile:
                self.current_wavfile.close()
                self.current_wavfile = None
        except Exception as e:
            logger.error("Error closing WAV file: %s", e)
        
        self.is_recording = False
        self.current_filepath = None
        self.current_wav_path = None
        
        # Check actual audio duration from WAV file size (more accurate than wall-clock)
        actual_duration = 0
        try:
            if wav_path and wav_path.exists():
                wav_size = wav_path.stat().st_size
                actual_duration = max(0, (wav_size - 44)) / 24000.0  # 12kHz * 16bit * mono
        except Exception:
            actual_duration = duration  # fallback to wall-clock
        
        logger.info("Recording stopped: %s (wall=%.1fs, audio=%.1fs, %d chunks)", 
                     filepath.name if filepath else "?", duration, actual_duration, self.chunk_count)
        
        if actual_duration < MIN_DURATION_SECONDS:
            try:
                if wav_path and wav_path.exists():
                    wav_path.unlink()
                    logger.info("Deleted short recording (%.1fs < %ds)", duration, MIN_DURATION_SECONDS)
            except Exception as e:
                logger.error("Error deleting short recording: %s", e)
        else:
            threading.Thread(
                target=self._convert_to_mp3,
                args=(wav_path, filepath),
                daemon=True
            ).start()
    
    def _convert_to_mp3(self, wav_path: Path, mp3_path: Path):
        """Convert WAV to MP3 using ffmpeg, embedding station name as ID3 metadata"""
        try:
            if not wav_path.exists():
                logger.error("WAV file not found: %s", wav_path)
                return
            
            cmd = [
                'ffmpeg', '-y', '-i', str(wav_path),
                '-codec:a', 'libmp3lame', '-qscale:a', '2',
            ]
            
            # Add ID3 metadata if station name is available
            station = getattr(self, '_recording_station_name', None)
            utc_str = getattr(self, '_recording_utc_str', '')
            freq_hz = self.current_frequency_hz
            freq_str = f"{freq_hz/1e6:.4f} MHz" if freq_hz else "unknown"
            
            if station:
                cmd.extend(['-metadata', f'title={station} - {freq_str}'])
                cmd.extend(['-metadata', f'artist={station}'])
                cmd.extend(['-metadata', f'album=OpenWebRX SDR Recordings'])
                cmd.extend(['-metadata', f'comment={freq_str} {utc_str}'])
            else:
                cmd.extend(['-metadata', f'title={freq_str} {utc_str}'])
                cmd.extend(['-metadata', f'album=OpenWebRX SDR Recordings'])
            
            cmd.append(str(mp3_path))
            
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60
            )
            
            if result.returncode == 0 and mp3_path.exists():
                size_kb = mp3_path.stat().st_size / 1024
                logger.info("Converted to MP3: %s (%.1f KB)", mp3_path.name, size_kb)
                wav_path.unlink()
            else:
                logger.error("ffmpeg conversion failed (code %d)", result.returncode)
                
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg timeout converting %s", wav_path)
        except Exception as e:
            logger.error("Error converting to MP3: %s", e)
    
    def _cancel_silence_timer(self):
        if self.silence_timer:
            self.silence_timer.cancel()
            self.silence_timer = None
    
    def _schedule_silence_timeout(self):
        self._cancel_silence_timer()
        self.silence_timer = threading.Timer(SILENCE_TIMEOUT, self._on_silence_timeout)
        self.silence_timer.daemon = True
        self.silence_timer.start()
    
    def _on_silence_timeout(self):
        with self._rec_lock:
            if self.is_recording and self.last_signal_time:
                elapsed = time.time() - self.last_signal_time
                if elapsed >= SILENCE_TIMEOUT:
                    logger.info("Silence detected for %.1fs - stopping recording", elapsed)
                    self._stop_recording()
    
    def _has_frequency_changed(self, frequency_hz: Optional[int]) -> bool:
        """Check if frequency changed compared to last seen value.
        Also updates the dwell-time tracker.
        Uses tolerance to ignore small variations and interleaved multi-decoder streams."""
        now = time.time()
        
        if frequency_hz is None:
            return False
        
        # Tolerance: ignore changes smaller than 50 kHz (different decoder offsets)
        FREQ_TOLERANCE_HZ = 50000
        
        if self._last_seen_freq is None:
            self._last_seen_freq = frequency_hz
            self._freq_stable_since = now
            return False
        
        if abs(self._last_seen_freq - frequency_hz) > FREQ_TOLERANCE_HZ:
            # Significant frequency change — reset dwell timer
            old_freq = self._last_seen_freq
            self._last_seen_freq = frequency_hz
            self._freq_stable_since = now
            logger.debug("Frequency changed: %.4f → %.4f MHz",
                         old_freq / 1e6, frequency_hz / 1e6)
            return True
        
        # Within tolerance — don't reset dwell timer, just update tracking
        return False
    
    def _frequency_dwelled(self) -> bool:
        """Return True if frequency has been stable for at least FREQ_DWELL_SECONDS."""
        if self._freq_stable_since is None:
            return False
        return (time.time() - self._freq_stable_since) >= FREQ_DWELL_SECONDS
    
    def set_station_info(self, name: Optional[str], frequency_hz: Optional[int] = None):
        """Set station/event name for the next recording.
        Called by the orchestrator before each dwell so that
        filenames and MP3 metadata contain the station name."""
        self._station_name = name
        if frequency_hz is not None:
            self.current_frequency_hz = frequency_hz

    def set_current_mode(self, mode: Optional[str]):
        """Set the current demodulation mode. Used to skip recording on digital modes."""
        if mode is None:
            return
        mode_lower = mode.lower()
        was_disabled = self._recording_disabled_for_mode
        self._current_mode = mode_lower
        self._recording_disabled_for_mode = mode_lower in DIGITAL_MODES
        if self._recording_disabled_for_mode and not was_disabled:
            logger.info("Recording disabled for digital mode: %s", mode)
            if self.is_recording:
                logger.info("Stopping recording — switched to digital mode %s", mode)
                self._stop_recording()
        elif not self._recording_disabled_for_mode and was_disabled:
            logger.info("Recording re-enabled for mode: %s", mode)

    def write_audio_chunk(self, audio_data: bytes, frequency_hz: Optional[int] = None, is_int16: bool = False):
        """
        Write audio chunk - receives PCM data from the DSP chain.
        Supports both FLOAT32 and INT16 input formats.
        Analyzes RMS, records when signal above threshold.
        Handles frequency changes: stops current recording, waits dwell time before new one.
        """
        if len(audio_data) < 4:
            return

        # Skip recording for digital modes
        if self._recording_disabled_for_mode:
            return
        
        if is_int16:
            # Data is already INT16 - compute RMS on int16 data
            rms = self._compute_rms_int16(audio_data)
            has_signal = rms > AUDIO_RMS_THRESHOLD
            int16_data_ready = audio_data
        else:
            # Data is FLOAT32 - compute RMS on float data
            rms = self._compute_rms_float(audio_data)
            has_signal = rms > AUDIO_RMS_THRESHOLD
            int16_data_ready = None  # will convert lazily
        
        with self._rec_lock:
            # --- Frequency change detection ---
            freq_changed = self._has_frequency_changed(frequency_hz)
            
            if freq_changed and self.is_recording:
                logger.info("Frequency changed while recording — stopping current recording")
                self._stop_recording()
            
            # --- Dwell time gate ---
            if not self._frequency_dwelled():
                # Not yet stable on this frequency — don't start recording
                return
            
            # Safety: enforce max recording duration
            if self.is_recording and self.recording_start_time:
                rec_elapsed = time.time() - self.recording_start_time
                if rec_elapsed >= MAX_RECORDING_DURATION:
                    logger.info("Max recording duration reached (%.0fs) — stopping", rec_elapsed)
                    self._stop_recording()
                    return

            if has_signal:
                if not self.is_recording:
                    self._start_recording(frequency_hz)
                
                self.last_signal_time = time.time()
                self.chunk_count += 1
                
                # Write to WAV (convert float32 to int16 if needed)
                try:
                    if self.current_wavfile:
                        if int16_data_ready is not None:
                            self.current_wavfile.writeframes(int16_data_ready)
                        else:
                            self.current_wavfile.writeframes(self._float32_to_int16(audio_data))
                except Exception as e:
                    logger.error("Error writing audio chunk: %s", e)
                
                self._schedule_silence_timeout()
                
            elif self.is_recording:
                self.chunk_count += 1
                try:
                    if self.current_wavfile:
                        if int16_data_ready is not None:
                            self.current_wavfile.writeframes(int16_data_ready)
                        else:
                            self.current_wavfile.writeframes(self._float32_to_int16(audio_data))
                except Exception as e:
                    logger.error("Error writing audio chunk: %s", e)
    
    def get_status(self) -> dict:
        status = {
            'recording': self.is_recording,
            'recording_disabled': self._recording_disabled_for_mode,
            'current_mode': self._current_mode,
        }
        if self.is_recording:
            status['duration'] = time.time() - self.recording_start_time
            status['file'] = self.current_filepath.name if self.current_filepath else None
            status['frequency_hz'] = self.current_frequency_hz
        return status
    
    def _status_broadcaster(self):
        import time as _time
        while True:
            try:
                _time.sleep(1.0)
                status = self.get_status()
                try:
                    from owrx.client import ClientRegistry
                    registry = ClientRegistry.getSharedInstance()
                    for client in list(registry.clients):
                        try:
                            if hasattr(client, 'write_recording_status'):
                                # Skip clients with dead websocket connections
                                conn = getattr(client, 'conn', None)
                                if conn is not None and (getattr(conn, 'socketError', False) or not getattr(conn, 'open', True)):
                                    continue
                                client.write_recording_status(status)
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass

    def _cleanup_worker(self):
        logger.info("Cleanup worker started (max age: %d days)", MAX_AGE_DAYS)
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
                for filepath in self.recordings_dir.glob("temp_*.wav"):
                    try:
                        if filepath.stat().st_mtime < time.time() - 3600:
                            filepath.unlink()
                            deleted_count += 1
                    except Exception as e:
                        logger.error("Error deleting temp %s: %s", filepath.name, e)
                if deleted_count > 0:
                    logger.info("Cleanup: deleted %d old files", deleted_count)
            except Exception as e:
                logger.error("Cleanup worker error: %s", e)


_recorder_instance = None

def get_recorder() -> SquelchRecorder:
    global _recorder_instance
    if _recorder_instance is None:
        _recorder_instance = SquelchRecorder()
    return _recorder_instance
