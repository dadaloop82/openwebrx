"""
Auto Mode Orchestrator for OpenWebRX
=====================================
Coordinates auto-mode components (ClientMonitor, AutoTuner, DecoderManager,
AutoRecorder).  Implements a **schedule-first** state machine:

  1. If calendar events (EIBI / priyom) are on-air right now  ->  scan through
     ALL of them round-robin, **60 s each**, like a real scanner.  Priyom
     (numbers stations) are scanned first.
  2. If NO calendar events are active  ->  cycle through the configured
     scan frequencies (APRS, FT8, etc.)
  3. When a remote client connects  ->  immediately stop everything and yield
     control to the user.

Signal quality rating
---------------------
After every 60 s dwell the system automatically rates the signal  0-5 stars or
"nr" (Non Ricevuto - squelch never opened / noise only).  Ratings are saved
as type "automatico" in /var/lib/openwebrx/signal_ratings.json.
After 5 consecutive "nr" a station is disabled and skipped.
"""

import os
import logging
import threading
import time
import json
import subprocess
import wave
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from scipy.signal import firwin, lfilter, lfilter_zi
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

logger = logging.getLogger(__name__)

# -- Paths ---------------------------------------------------------------
SCHEDULE_EVENTS_JSON = "/var/www/html/sdr-eibi-events.json"
RATINGS_DB_PATH = "/var/lib/openwebrx/signal_ratings.json"

# -- Geographic filter: EIBI target regions receivable from Bolzano (46.5°N 11.3°E)
# Discone antenna + LNA, RTL-SDR direct sampling 3-30 MHz
BOLZANO_RECEIVABLE_TARGETS = {
    'Eu', 'CEu', 'WEu', 'SEu', 'EEu', 'NEu', 'SEE',  # Europe
    'ME', 'NAf',                                        # Near East, North Africa
    'CIS', 'Cau', 'UKR', 'RUS',                        # CIS / ex-USSR
    'NAO', 'Global', 'ITN',                             # Atlantic, Global, Italy
}

# -- Tuning constants -----------------------------------------------------
EVENT_DWELL_SECONDS = 60                           # dwell per event in scan
SIGNAL_SAMPLE_INTERVAL = 3                         # seconds between samples
NR_DISABLE_THRESHOLD = 5                           # consecutive "nr" -> disable
MAX_EVENTS_PER_CYCLE = 20                          # max events per scan cycle
LONG_EVENT_THRESHOLD_MIN = 120                     # events >2h = "long-running"

# -- Recording budget: max 30 min of recording per 2-hour rolling window per station
REC_BUDGET_SECONDS = 30 * 60                       # 30 minutes max per window
REC_WINDOW_SECONDS = 2 * 3600                      # 2-hour rolling window

# -- IQ signal detection ---------------------------------------------------
IQ_FFT_SIZE = 2048                                 # FFT bins for spectral analysis
IQ_SNR_THRESHOLD = 2.5                             # signal/noise ratio to count as "signal present"
IQ_ANALYSIS_INTERVAL = 1.0                         # seconds between FFT analyses in the reader thread


# ========================================================================
#  IQ Power Monitor  –  reads raw IQ from SDR source, detects signal
# ========================================================================

class IQPowerMonitor:
    """Background thread that reads raw IQ data from the SDR source buffer
    and continuously computes a spectral SNR at center frequency.

    This works in auto-mode even without any WebSocket client / DSP chain.
    """

    def __init__(self):
        self._reader = None
        self._thread = None
        self._running = False
        self._signal_detected = False
        self._snr = 0.0
        self._avg_snr_sum = 0.0
        self._avg_snr_count = 0
        self._lock = threading.Lock()

    def start(self, source):
        """Begin reading IQ from source. Safe to call if already running."""
        self.stop()
        if not HAS_NUMPY:
            logger.warning("IQPowerMonitor: numpy not available – signal detection disabled")
            return
        try:
            buf = source.getBuffer()
            self._reader = buf.getReader()
        except Exception as e:
            logger.warning("IQPowerMonitor: cannot get IQ buffer reader: %s", e)
            return
        self._running = True
        self._signal_detected = False
        self._snr = 0.0
        self._avg_snr_sum = 0.0
        self._avg_snr_count = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="iq_power_monitor")
        self._thread.start()
        logger.debug("IQPowerMonitor started")

    def _run(self):
        fft_size = IQ_FFT_SIZE
        last_analysis = 0.0
        bytes_per_sample = 8  # complex float32 = 2 × 4 bytes

        while self._running:
            try:
                data = self._reader.read()
                if data is None:
                    break

                now = time.time()
                if now - last_analysis < IQ_ANALYSIS_INTERVAL:
                    continue  # skip, wait until next analysis window
                last_analysis = now

                raw = data.tobytes() if hasattr(data, 'tobytes') else bytes(data)
                needed = fft_size * bytes_per_sample
                if len(raw) < needed:
                    continue

                # Use the LAST fft_size samples from the chunk (freshest data)
                iq = np.frombuffer(raw[-needed:], dtype=np.complex64)

                # Windowed FFT for better spectral leakage control
                window = np.hanning(fft_size)
                spectrum = np.abs(np.fft.fftshift(np.fft.fft(iq * window))) ** 2

                # Signal power: center ±2% of bandwidth (covers ~10–20 kHz depending on samp_rate)
                center = fft_size // 2
                half_sig = max(4, fft_size // 50)  # ~2% of bins each side
                sig_slice = spectrum[center - half_sig : center + half_sig]
                signal_power = float(np.mean(sig_slice))

                # Noise power: outer quarters (away from center signal)
                quarter = fft_size // 4
                noise_power = float((np.mean(spectrum[:quarter]) + np.mean(spectrum[-quarter:])) / 2)

                snr = signal_power / max(noise_power, 1e-30)

                with self._lock:
                    self._snr = snr
                    self._signal_detected = snr > IQ_SNR_THRESHOLD
                    self._avg_snr_sum += snr
                    self._avg_snr_count += 1

            except (ValueError, BrokenPipeError, OSError):
                break
            except Exception as e:
                logger.debug("IQPowerMonitor read error: %s", e)
                break

        logger.debug("IQPowerMonitor thread exiting")

    # -- public query methods --
    def has_signal(self) -> bool:
        with self._lock:
            return self._signal_detected

    def get_snr(self) -> float:
        with self._lock:
            return self._snr

    def get_avg_snr(self) -> float:
        with self._lock:
            if self._avg_snr_count == 0:
                return 0.0
            return self._avg_snr_sum / self._avg_snr_count

    def reset_avg(self):
        with self._lock:
            self._avg_snr_sum = 0.0
            self._avg_snr_count = 0

    def stop(self):
        self._running = False
        if self._reader is not None:
            try:
                self._reader.stop()
            except Exception:
                pass
            self._reader = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


# ========================================================================
#  Headless Recorder  –  demodulates IQ → audio → MP3 without DSP chain
# ========================================================================

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
HEADLESS_AUDIO_RATE = 12000       # output sample rate
HEADLESS_MAX_DURATION = 120       # max seconds per recording
HEADLESS_MIN_DURATION = 3         # discard recordings shorter than this
HEADLESS_CHUNK_SAMPLES = 48000    # process IQ in 48K-sample blocks (~20 ms at 2.4 MHz)


class HeadlessRecorder:
    """Records demodulated audio directly from raw IQ data.

    Creates its own buffer reader from the SDR source, demodulates
    (AM / USB / LSB / NFM) using numpy, decimates with scipy FIR
    filters to 12 kHz, writes WAV, converts to MP3 via ffmpeg.

    This enables headless recording in auto-mode without any
    WebSocket client or DSP chain running.
    """

    def __init__(self):
        self._thread = None
        self._stop_event = threading.Event()
        self._reader = None
        self._mp3_name = None
        self._lock = threading.Lock()

    @property
    def last_recording(self):
        """Return the filename of the last completed recording (or None)."""
        with self._lock:
            return self._mp3_name

    def start(self, source, freq_hz, mode, station_name, samp_rate=2400000):
        """Start recording from source IQ buffer."""
        if not HAS_NUMPY or not HAS_SCIPY:
            logger.warning("HeadlessRecorder: numpy/scipy not available")
            return
        if self._thread and self._thread.is_alive():
            self.stop()

        with self._lock:
            self._mp3_name = None
        self._stop_event.clear()

        try:
            self._reader = source.getBuffer().getReader()
        except Exception as e:
            logger.warning("HeadlessRecorder: cannot get reader: %s", e)
            return

        self._thread = threading.Thread(
            target=self._record_loop,
            args=(freq_hz, mode, station_name, samp_rate),
            daemon=True,
            name="headless_recorder",
        )
        self._thread.start()
        logger.info("HeadlessRecorder started for %s (%.3f MHz, %s)",
                     station_name or "?", freq_hz / 1e6, mode)

    def stop(self):
        """Stop recording.  Blocks until finalized.  Returns MP3 filename or None."""
        self._stop_event.set()
        if self._reader is not None:
            try:
                self._reader.stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        self._reader = None
        with self._lock:
            return self._mp3_name

    # ------------------------------------------------------------------ #
    #  Internal recording loop                                            #
    # ------------------------------------------------------------------ #

    def _record_loop(self, freq_hz, mode, station_name, samp_rate):
        mode = (mode or "am").lower()
        dec_factor = max(1, samp_rate // HEADLESS_AUDIO_RATE)  # e.g. 200

        # Build multi-stage decimation filters
        stages = self._design_decimation(dec_factor, samp_rate)

        # Prepare WAV output
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        freq_mhz = freq_hz / 1e6
        wav_path = os.path.join(RECORDINGS_DIR, "temp_headless_{}.wav".format(ts_str))

        wf = wave.open(wav_path, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(HEADLESS_AUDIO_RATE)

        total_audio_samples = 0
        max_audio_samples = HEADLESS_MAX_DURATION * HEADLESS_AUDIO_RATE
        iq_accum = []
        iq_accum_len = 0

        try:
            while not self._stop_event.is_set() and total_audio_samples < max_audio_samples:
                data = self._reader.read()
                if data is None:
                    break

                raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
                if len(raw) < 8:
                    continue

                chunk = np.frombuffer(raw, dtype=np.complex64)
                iq_accum.append(chunk)
                iq_accum_len += len(chunk)

                # Process when we have enough data
                if iq_accum_len < HEADLESS_CHUNK_SAMPLES:
                    continue

                iq_block = np.concatenate(iq_accum)
                iq_accum = []
                iq_accum_len = 0

                # 1. Demodulate at full sample rate
                audio_full = self._demodulate(iq_block, mode)

                # 2. Multi-stage decimate to HEADLESS_AUDIO_RATE
                audio_dec = audio_full
                for stage in stages:
                    fir_coeffs, factor = stage[0], stage[1]
                    if len(audio_dec) < factor:
                        break
                    filtered, stage[2] = lfilter(fir_coeffs, 1.0, audio_dec, zi=stage[2])
                    audio_dec = filtered[::factor]

                if len(audio_dec) == 0:
                    continue

                # 3. Normalize & convert to int16
                peak = np.max(np.abs(audio_dec))
                if peak > 1e-10:
                    audio_dec = audio_dec / peak * 0.7
                samples_i16 = (audio_dec * 32767).clip(-32767, 32767).astype(np.int16)
                wf.writeframes(samples_i16.tobytes())
                total_audio_samples += len(samples_i16)

        except Exception as e:
            logger.error("HeadlessRecorder loop error: %s", e, exc_info=True)
        finally:
            wf.close()

        # Finalize: convert WAV → MP3
        duration = total_audio_samples / max(HEADLESS_AUDIO_RATE, 1)
        if duration < HEADLESS_MIN_DURATION:
            logger.info("HeadlessRecorder: discarded short recording (%.1fs)", duration)
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            return

        mp3_name = "{:.4f}MHz_{}_{}.mp3".format(freq_mhz, ts_str[:8], ts_str[9:])
        mp3_path = os.path.join(RECORDINGS_DIR, mp3_name)
        title = station_name or "Unknown"
        comment = "{:.4f} MHz - {} UTC".format(
            freq_mhz, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-i", wav_path,
                 "-codec:a", "libmp3lame", "-qscale:a", "2",
                 "-metadata", "title={}".format(title),
                 "-metadata", "artist=OpenWebRX Auto-Mode",
                 "-metadata", "comment={}".format(comment),
                 mp3_path],
                capture_output=True, timeout=60,
            )
        except Exception as e:
            logger.error("HeadlessRecorder ffmpeg error: %s", e)

        # Cleanup WAV
        try:
            os.unlink(wav_path)
        except OSError:
            pass

        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
            logger.info("HeadlessRecorder: saved %s (%.1fs)", mp3_name, duration)
            with self._lock:
                self._mp3_name = mp3_name
        else:
            logger.warning("HeadlessRecorder: MP3 conversion failed for %s", wav_path)

    # ------------------------------------------------------------------ #
    #  Demodulation                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _demodulate(iq, mode):
        """Demodulate complex IQ to real audio signal."""
        if mode == "am":
            audio = np.abs(iq)
            audio -= np.mean(audio)     # remove DC
            return audio.astype(np.float64)
        elif mode == "usb":
            return np.real(iq).astype(np.float64)
        elif mode == "lsb":
            # LSB: spectral inversion via negated imaginary part
            return -np.imag(iq).astype(np.float64)
        elif mode in ("nfm", "wfm"):
            # FM discriminator: phase difference between successive samples
            if len(iq) < 2:
                return np.zeros(1, dtype=np.float64)
            disc = iq[1:] * np.conj(iq[:-1])
            return np.angle(disc).astype(np.float64)
        else:
            # Fallback: AM envelope
            audio = np.abs(iq)
            audio -= np.mean(audio)
            return audio.astype(np.float64)

    # ------------------------------------------------------------------ #
    #  Multi-stage FIR decimation design                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _design_decimation(total_factor, samp_rate):
        """Design a multi-stage FIR decimation chain.

        Factorizes `total_factor` into stages of <= 10 each.
        Returns list of [fir_coeffs, factor, zi_state] triples.
        """
        # Factorize into stages
        factors = []
        remaining = total_factor
        for div in [10, 8, 5, 4, 3, 2]:
            while remaining >= div and remaining % div == 0:
                factors.append(div)
                remaining //= div
        if remaining > 1:
            factors.append(remaining)
        if not factors:
            factors = [1]

        stages = []
        current_rate = samp_rate
        for f in factors:
            # Anti-aliasing LPF: cutoff at 0.8 × new_nyquist
            new_rate = current_rate / f
            cutoff_norm = 0.8 * (new_rate / 2) / (current_rate / 2)
            cutoff_norm = min(cutoff_norm, 0.95)  # safety clamp
            # More taps for narrower cutoffs
            ntaps = max(32, int(4.0 / cutoff_norm))
            ntaps = min(ntaps, 512)
            if ntaps % 2 == 0:
                ntaps += 1  # odd number of taps for type-I FIR
            fir = firwin(ntaps, cutoff_norm)
            zi = lfilter_zi(fir, 1.0) * 0.0
            stages.append([fir, f, zi])
            current_rate = new_rate

        logger.debug("HeadlessRecorder decimation: %s (total %dx, %d Hz → %d Hz)",
                     "×".join(str(f) for _, f, _ in stages),
                     total_factor, samp_rate, int(current_rate))
        return stages


class AutoModeState(Enum):
    MANUAL = "manual"
    IDLE   = "idle"
    AUTO   = "auto"


# ========================================================================
#  Signal-quality helpers
# ========================================================================

def _station_key(event: Dict) -> str:
    """Unique key for a station: freq + description."""
    return "{}|{}".format(event.get("frequency_mhz", "0"),
                          event.get("description", "?"))


def _signal_ratio_to_stars(ratio: float) -> int:
    """Map fraction-of-dwell-with-signal (0.0-1.0) to 0-5 stars."""
    if ratio <= 0.0:
        return 0
    if ratio < 0.05:
        return 1
    if ratio < 0.15:
        return 2
    if ratio < 0.35:
        return 3
    if ratio < 0.60:
        return 4
    return 5


# ========================================================================
#  Offline Audio Quality Analyzer  –  no AI, pure DSP metrics
# ========================================================================

RECORDINGS_DIR_AQ = "/var/lib/openwebrx/recordings"
_AQ_SAMPLE_RATE = 16000  # downsample for fast analysis


def analyze_recording_quality(mp3_path: str) -> Optional[Dict[str, Any]]:
    """Analyze an MP3 recording for audio quality using spectral metrics.

    Returns a dict with:
      - audio_score: int 1-5 (1=pure noise, 5=clean signal)
      - metrics: {spectral_flatness, spectral_entropy, modulation_index,
                  autocorrelation_peak, crest_factor}
      - noise_level: float 0.0 (clean) - 1.0 (pure noise)
    Returns None if analysis fails.
    """
    if not HAS_NUMPY or not HAS_SCIPY:
        return None
    from scipy.signal import spectrogram as sp_spectrogram

    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", mp3_path,
            "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1",
            "-ar", str(_AQ_SAMPLE_RATE), "-"
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode != 0 or len(r.stdout) < _AQ_SAMPLE_RATE * 2:
            return None
        pcm = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception as e:
        logger.debug("Audio quality: failed to decode %s: %s", mp3_path, e)
        return None

    if len(pcm) < _AQ_SAMPLE_RATE:
        return None

    eps = 1e-10
    sr = _AQ_SAMPLE_RATE

    try:
        # 1. Spectral flatness (Wiener entropy): noise → ~1.0, tonal → low
        nperseg = min(2048, len(pcm) // 4)
        f, t, Sxx = sp_spectrogram(pcm, fs=sr, nperseg=nperseg, noverlap=nperseg // 2)
        log_mean = np.mean(np.log(Sxx + eps), axis=0)
        arith_mean = np.log(np.mean(Sxx + eps, axis=0))
        flatness = float(np.mean(np.exp(log_mean - arith_mean)))

        # 2. Spectral entropy (noise → high, tonal → low)
        Sxx_norm = Sxx / (np.sum(Sxx, axis=0, keepdims=True) + eps)
        ent_frames = -np.sum(Sxx_norm * np.log2(Sxx_norm + eps), axis=0)
        max_ent = np.log2(Sxx.shape[0])
        entropy = float(np.mean(ent_frames)) / max_ent  # normalized 0-1

        # 3. Modulation index (speech/music > 0.15, noise < 0.10)
        frame_len = sr // 10  # 100ms frames
        n_frames = len(pcm) // frame_len
        if n_frames < 2:
            return None
        rms_frames = np.array([
            np.sqrt(np.mean(pcm[i * frame_len:(i + 1) * frame_len] ** 2))
            for i in range(n_frames)
        ])
        mod_index = float(np.std(rms_frames) / (np.mean(rms_frames) + eps))

        # 4. Autocorrelation peak (periodic signal → high, noise → low)
        chunk = pcm[:sr * 2]  # first 2 seconds
        ac = np.correlate(chunk, chunk, "full")
        ac = ac[len(ac) // 2:]
        ac = ac / (ac[0] + eps)
        ac_trimmed = ac[50:800]  # look for periodicity in 20Hz-320Hz range
        ac_peak = float(np.max(ac_trimmed)) if len(ac_trimmed) > 0 else 0.0

        # 5. Crest factor (noise tends to be high ~5+, clean speech ~3.5-4)
        overall_rms = float(np.sqrt(np.mean(pcm ** 2)))
        peak = float(np.max(np.abs(pcm)))
        crest = peak / (overall_rms + eps)

    except Exception as e:
        logger.debug("Audio quality analysis error for %s: %s", mp3_path, e)
        return None

    # -- Composite noise score (0=clean, 1=pure noise) -- v4 --
    # Root cause analysis from real Bolzano HF data:
    #   ALL "noise" recordings have mod_index 0.04-0.13 (barely modulated carriers).
    #   A real voice broadcast has mod_index > 0.20.
    #   Therefore mod_index MUST be the dominant metric (50% weight).
    #   v4: pure noise (noise_level >= 0.45) now correctly gets 0 stars.
    #
    # Normalization:
    #   mod:   0.04 (silence/carrier) → noise=1.0,   0.22+ (voice) → noise 0.0
    #   ent:   0.30 (clean)           → noise=0.0,   0.90 (white)  → noise=1.0
    #   ac:    1.0  (very periodic)   → noise=0.0,   0.0  (random) → noise=1.0
    #   flat:  0.0  (tonal)           → noise=0.0,   0.02+(white)  → noise=1.0
    #   crest: 3.0  (normal audio)    → noise=0.0,   5.5+ (noisy)  → noise=1.0

    ac_noise   = 1.0 - min(1.0, max(0.0, ac_peak))
    ent_noise  = min(1.0, max(0.0, (entropy - 0.3) / 0.6))
    flat_noise = min(1.0, max(0.0, flatness / 0.02))
    # Key: mod_index < 0.04 is pure carrier/silence, > 0.22 is well-modulated voice
    mod_noise  = 1.0 - min(1.0, max(0.0, (mod_index - 0.04) / 0.18))
    crest_noise = min(1.0, max(0.0, (crest - 3.0) / 2.5))

    noise_level = (
        ac_noise   * 0.10 +
        ent_noise  * 0.25 +
        flat_noise * 0.05 +
        mod_noise  * 0.50 +   # dominant: low modulation = noise
        crest_noise * 0.10
    )
    noise_level = min(1.0, max(0.0, noise_level))

    # Map to 0-5 stars (v4 – very strict, 0 stars for pure noise)
    # Verified on real Bolzano data: HF noise/carriers score 0.49-0.65 → 0 stars
    # mod_index < 0.15 with noise_level > 0.45 = definitely just noise
    if noise_level >= 0.45:
        audio_score = 0    # pure noise / interference / empty carrier
    elif noise_level >= 0.35:
        audio_score = 1    # mostly noise, faint traces of signal
    elif noise_level >= 0.25:
        audio_score = 2    # noisy but some content audible
    elif noise_level >= 0.15:
        audio_score = 3    # fair, audible with background noise
    elif noise_level >= 0.08:
        audio_score = 4    # good quality
    else:
        audio_score = 5    # excellent, clean signal

    return {
        "audio_score": audio_score,
        "noise_level": round(noise_level, 3),
        "metrics": {
            "spectral_flatness": round(flatness, 4),
            "spectral_entropy": round(entropy, 4),
            "modulation_index": round(mod_index, 4),
            "autocorrelation_peak": round(ac_peak, 4),
            "crest_factor": round(crest, 2),
        }
    }


class SignalRatingsDB:
    """Thread-safe persistent store for signal-quality ratings.

    Schema per station_key:
        {
            "ratings": [
                {"score": 3, "type": "automatico", "ts": "2026-02-28T07:15:00Z"},
                {"score": "nr", "type": "automatico", "ts": "..."},
                {"score": 4, "type": "manuale",     "ts": "..."},
            ],
            "avg_score": 2.5,
            "enabled": true,
            "consecutive_nr": 0
        }
    """

    def __init__(self, path=RATINGS_DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._db = {}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as f:
                    self._db = json.load(f)
            except Exception as e:
                logger.warning("Ratings DB load failed: %s - starting fresh", e)
                self._db = {}

    def _save(self):
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._db, f, indent=1, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error("Failed to save ratings DB: %s", e)

    def add_rating(self, station_key, score, rating_type="automatico", recording=None, audio_quality=None):
        """Add a rating.  score = int 0-5 or string 'nr'."""
        with self._lock:
            entry = self._db.setdefault(station_key, {
                "ratings": [], "avg_score": None, "enabled": True,
                "consecutive_nr": 0,
            })
            rating_entry = {
                "score": score,
                "type": rating_type,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if recording:
                rating_entry["recording"] = recording
            if audio_quality:
                rating_entry["audio_quality"] = audio_quality
            entry["ratings"].append(rating_entry)
            # Keep last 50 ratings max
            if len(entry["ratings"]) > 50:
                entry["ratings"] = entry["ratings"][-50:]

            # Update consecutive_nr
            # Scores of "nr" or ≤2 stars (mostly noise) count as "not received"
            if score == "nr" or (isinstance(score, (int, float)) and score <= 2):
                entry["consecutive_nr"] = entry.get("consecutive_nr", 0) + 1
            else:
                entry["consecutive_nr"] = 0

            # Auto-disable after threshold
            if entry["consecutive_nr"] >= NR_DISABLE_THRESHOLD:
                if entry.get("enabled", True):
                    logger.info("DISABLED station '%s' after %d consecutive 'Segnale assente'",
                                station_key, NR_DISABLE_THRESHOLD)
                entry["enabled"] = False

            # Recompute average (numeric scores only)
            numeric = [r["score"] for r in entry["ratings"]
                       if isinstance(r["score"], (int, float))]
            entry["avg_score"] = round(sum(numeric) / len(numeric), 2) if numeric else None

            self._save()

    def is_enabled(self, station_key):
        with self._lock:
            entry = self._db.get(station_key)
            if entry is None:
                return True
            return entry.get("enabled", True)

    def get_entry(self, station_key):
        with self._lock:
            return self._db.get(station_key)

    def get_all(self):
        with self._lock:
            return dict(self._db)

    def set_enabled(self, station_key, enabled):
        """Manually enable/disable a station."""
        with self._lock:
            entry = self._db.setdefault(station_key, {
                "ratings": [], "avg_score": None, "enabled": enabled,
                "consecutive_nr": 0,
            })
            entry["enabled"] = enabled
            if enabled:
                entry["consecutive_nr"] = 0
            self._save()


# ========================================================================
#  Orchestrator
# ========================================================================

class AutoModeOrchestrator:
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

        # Components (set externally via set_components)
        self.client_monitor = None
        self.auto_tuner = None
        self.decoder_manager = None
        self.auto_recorder = None

        # Scan state
        self.current_frequency_index = 0
        self.frequencies = []
        self.saved_user_settings = None

        # Event scan state (round-robin index)
        self._event_scan_index = 0

        # Current scan tracking (for status API / widget)
        self._current_scan_info = None  # dict with freq, mode, label, source, event_index, event_total
        self._scanning_events = False   # True when scanning events, False when scanning frequencies
        self._last_quality = None       # Last quality measurement result for widget

        # Ratings
        self.ratings_db = SignalRatingsDB()

        # Recording budget tracker: station_key -> {"window_start": float, "used_sec": float}
        self._rec_budgets: dict = {}

        # IQ-based signal detector (works without DSP chain / WebSocket clients)
        self._iq_monitor = IQPowerMonitor()

        # Headless recorder (demodulates IQ → audio → MP3 without DSP chain)
        self._headless_rec = HeadlessRecorder()

        logger.info("AutoModeOrchestrator initialized (IQ monitor: numpy=%s, headless rec: scipy=%s)",
                     HAS_NUMPY, HAS_SCIPY)

    # -- config ----------------------------------------------------------
    def _load_config(self):
        default_config = {
            "enabled": True,
            "frequencies": [
                {
                    "frequency": 145800000,
                    "mode": "NFM",
                    "squelch": 0.15,
                    "bandwidth": 12500,
                    "dwell_time": 60,
                    "label": "APRS 2m",
                }
            ],
            "cycle_mode": "sequential",
            "enable_recording": True,
            "enable_decoders": True,
            "transition_delay": 2,
        }
        try:
            from owrx.config.core import CoreConfig
            cfg_file = os.path.join(
                CoreConfig().get_data_directory(), "auto_mode_config.json"
            )
            if os.path.exists(cfg_file):
                with open(cfg_file, "r") as f:
                    data = json.load(f)
                    return data.get("orchestrator", default_config)
        except Exception as e:
            logger.debug("Using default orchestrator config: %s", e)
        return default_config

    # -- components ------------------------------------------------------
    def set_components(self, client_monitor=None, auto_tuner=None,
                       decoder_manager=None, auto_recorder=None):
        if client_monitor:
            self.client_monitor = client_monitor
            client_monitor.register_callback(
                "all_remote_clients_gone", self._on_clients_gone)
            client_monitor.register_callback(
                "remote_client_connected", self._on_client_connected)
        if auto_tuner:
            self.auto_tuner = auto_tuner
        if decoder_manager:
            self.decoder_manager = decoder_manager
        if auto_recorder:
            self.auto_recorder = auto_recorder
        logger.info("Components registered with orchestrator")

    # -- start / stop ----------------------------------------------------
    def start(self):
        if not self.config["enabled"]:
            logger.info("AutoModeOrchestrator disabled in config")
            return
        if self.running:
            logger.warning("AutoModeOrchestrator already running")
            return
        self.frequencies = self.config.get("frequencies", [])
        self.running = True
        self.orchestrator_thread = threading.Thread(
            target=self._orchestrator_loop, daemon=True)
        self.orchestrator_thread.start()
        logger.info("=====================================================")
        logger.info("AUTO MODE ORCHESTRATOR STARTED")
        logger.info("   Scan freqs: %d | Event dwell: %ds",
                     len(self.frequencies), EVENT_DWELL_SECONDS)
        logger.info("=====================================================")

    def stop(self):
        self.running = False
        if self.state == AutoModeState.AUTO:
            self._exit_auto_mode()
        if self.orchestrator_thread:
            self.orchestrator_thread.join(timeout=10)
        logger.info("AutoModeOrchestrator stopped")

    # -- client callbacks ------------------------------------------------
    def _on_clients_gone(self):
        if self.state == AutoModeState.MANUAL:
            logger.info("Clients gone -> AUTO mode")
            self._enter_auto_mode()

    def _on_client_connected(self, client=None):
        if self.state == AutoModeState.AUTO:
            logger.info("Client connected -> MANUAL mode")
            self._exit_auto_mode()

    def _enter_auto_mode(self):
        try:
            if self.auto_tuner:
                self.saved_user_settings = self.auto_tuner.get_current_settings()
            self.state = AutoModeState.AUTO
            if self.auto_tuner:
                self.auto_tuner.enter_auto_mode()
            self.current_frequency_index = 0
            self._event_scan_index = 0
            logger.info("=====================================================")
            logger.info("ENTERED AUTO MODE")
            logger.info("=====================================================")
        except Exception as e:
            logger.error("Error entering auto mode: %s", e, exc_info=True)
            self.state = AutoModeState.MANUAL

    def _exit_auto_mode(self):
        try:
            old = self.state
            self.state = AutoModeState.MANUAL
            if old != AutoModeState.AUTO:
                return

            # Stop IQ power monitor
            self._iq_monitor.stop()

            if self.decoder_manager:
                self.decoder_manager.stop_session()
            if self.auto_recorder:
                try:
                    if hasattr(self.auto_recorder, "stop_recording"):
                        self.auto_recorder.stop_recording()
                except Exception:
                    pass
            # Stop squelch recorder explicitly
            try:
                from owrx.auto_squelch_recorder import SquelchRecorder
                rec = SquelchRecorder()
                if rec.is_recording:
                    rec._stop_recording()
                    logger.info("Squelch recorder stopped on auto mode exit")
            except Exception as e:
                logger.debug("Squelch recorder stop on exit: %s", e)
            self.saved_user_settings = None
            if self.auto_tuner:
                self.auto_tuner.exit_auto_mode()
            logger.info("=====================================================")
            logger.info("EXITED AUTO MODE - User control restored")
            logger.info("=====================================================")
        except Exception as e:
            logger.error("Error exiting auto mode: %s", e, exc_info=True)

    # ====================================================================
    #  Schedule helpers
    # ====================================================================

    def _get_active_schedule_events(self):
        """Return ALL currently on-air events, priyom first, then EIBI.
        Disabled stations (5+ consecutive nr) are excluded."""
        if not os.path.exists(SCHEDULE_EVENTS_JSON):
            return []
        try:
            with open(SCHEDULE_EVENTS_JSON, "r") as f:
                data = json.load(f)
            events = data.get("events", []) if isinstance(data, dict) else data

            now_utc = datetime.now(timezone.utc)
            now_min = now_utc.hour * 60 + now_utc.minute
            day_min = 24 * 60

            active_priyom = []
            active_eibi = []

            for ev in events:
                ts = ev.get("time_utc", "")
                if ":" not in ts:
                    continue
                try:
                    h, m = map(int, ts.split(":"))
                except (ValueError, TypeError):
                    continue
                start = h * 60 + m

                # Use end_utc if available, else fall back to duration_min or default
                end_str = ev.get("end_utc", "")
                dur_min = ev.get("duration_min")
                source = ev.get("source", "EIBI")

                # Skip EIBI events not receivable from Bolzano
                if source == "EIBI":
                    target = ev.get("target", "")
                    # Handle compound targets like "Eu,NAf" or "CEu/SEu"
                    if target:
                        target_parts = [t.strip() for t in target.replace('/', ',').split(',')]
                        if not any(tp in BOLZANO_RECEIVABLE_TARGETS for tp in target_parts):
                            continue

                if end_str and ":" in end_str:
                    try:
                        eh, em = map(int, end_str.split(":"))
                        end = eh * 60 + em
                        # Normalize midnight-crossing: if end < start, it wraps past 00:00
                        if end < start:
                            end += day_min
                    except (ValueError, TypeError):
                        end = start + (dur_min if dur_min else 30)
                elif dur_min:
                    end = start + dur_min
                else:
                    # Fallback defaults
                    end = start + (15 if source == "priyom" else 30)

                # circular day check
                if end <= day_min:
                    on_air = start <= now_min < end
                else:
                    on_air = now_min >= start or now_min < (end % day_min)

                if not on_air:
                    continue

                # Skip disabled stations
                key = _station_key(ev)
                if not self.ratings_db.is_enabled(key):
                    continue

                if source == "priyom":
                    active_priyom.append(ev)
                else:
                    active_eibi.append(ev)

            # Sort EIBI by duration: short events first (time-sensitive)
            active_eibi.sort(key=lambda e: e.get("duration_min", 30))

            # Split EIBI into short (time-sensitive) and long (background)
            eibi_short = [e for e in active_eibi
                          if e.get("duration_min", 30) <= LONG_EVENT_THRESHOLD_MIN]
            eibi_long = [e for e in active_eibi
                         if e.get("duration_min", 30) > LONG_EVENT_THRESHOLD_MIN]

            # Priority: priyom > short EIBI > a few long EIBI
            result = active_priyom + eibi_short
            # Add some long events if we have room
            slots_left = max(0, MAX_EVENTS_PER_CYCLE - len(result))
            if slots_left > 0 and eibi_long:
                # Rotate which long events we sample using the scan index
                start_idx = self._event_scan_index % max(len(eibi_long), 1)
                for i in range(min(slots_left, len(eibi_long))):
                    idx = (start_idx + i) % len(eibi_long)
                    result.append(eibi_long[idx])

            if len(result) > MAX_EVENTS_PER_CYCLE:
                result = result[:MAX_EVENTS_PER_CYCLE]

            return result

        except Exception as e:
            logger.error("Error reading schedule events: %s", e, exc_info=True)
            return []

    # ====================================================================
    #  Signal quality measurement
    # ====================================================================

    def _measure_signal_during_dwell(self, dwell_seconds,
                                     source=None, freq_hz=0, mode="am",
                                     station_name=None):
        """Dwell for dwell_seconds while measuring signal via IQ power analysis.

        Uses two methods in parallel:
          1. IQPowerMonitor – reads raw IQ from the SDR source buffer and
             computes spectral SNR via numpy FFT.  Works headlessly in auto-mode.
          2. SquelchRecorder – checks if demodulated audio had signal.  Only
             works when a WebSocket client is connected (DSP chain running).

        When signal is first detected, also starts HeadlessRecorder to capture
        demodulated audio for playback.

        Returns (signal_ratio, total_samples):
            signal_ratio = fraction of samples where signal was detected
            total_samples = number of samples taken
        """
        signal_count = 0
        total_samples = 0
        end_time = time.time() + dwell_seconds
        headless_started = False

        # Reset average SNR for this dwell
        self._iq_monitor.reset_avg()

        while time.time() < end_time and self.state == AutoModeState.AUTO:
            time.sleep(SIGNAL_SAMPLE_INTERVAL)
            total_samples += 1

            detected = False

            # Method 1: IQ power monitor (primary – works without DSP chain)
            if self._iq_monitor.has_signal():
                detected = True

            # Method 2: SquelchRecorder fallback (works when user is connected)
            if not detected:
                try:
                    from owrx.auto_squelch_recorder import SquelchRecorder
                    rec = SquelchRecorder()
                    if rec.is_recording:
                        detected = True
                except Exception:
                    pass

            if detected:
                signal_count += 1
                # Start headless recorder on first signal detection (with budget check)
                if not headless_started and source is not None and HAS_SCIPY:
                    # Check budget for headless recording too
                    budget_ok = True
                    if station_name:
                        skey = "{}|{}".format(freq_hz / 1e6, station_name)
                        budget_ok = self._rec_budget_ok(skey)
                    if budget_ok:
                        try:
                            try:
                                samp_rate = source.getProps()["samp_rate"]
                            except (KeyError, TypeError):
                                samp_rate = 2400000
                            self._headless_rec.start(source, freq_hz, mode,
                                                      station_name, samp_rate)
                            headless_started = True
                        except Exception as e:
                            logger.warning("HeadlessRecorder start failed: %s", e)
                    else:
                        logger.info("SKIP headless recording — budget exhausted for '%s'", station_name)

        # Stop headless recorder (finishes WAV → MP3 conversion)
        rec_filename = None
        if headless_started:
            rec_filename = self._headless_rec.stop()
            # Charge headless recording time to budget
            if station_name:
                skey = "{}|{}".format(freq_hz / 1e6, station_name)
                self._rec_budget_add(skey, dwell_seconds)

        ratio = signal_count / max(total_samples, 1)
        avg_snr = self._iq_monitor.get_avg_snr()
        logger.info("  Signal measurement: %d/%d detected (ratio=%.2f, avg_snr=%.1f, rec=%s)",
                     signal_count, total_samples, ratio, avg_snr,
                     rec_filename or "none")
        return ratio, total_samples, rec_filename

    def _rate_and_save(self, event, signal_ratio, recording=None):
        """Compute rating from signal_ratio + offline audio analysis and persist."""
        key = _station_key(event)
        avg_snr = self._iq_monitor.get_avg_snr()

        # --- Offline audio quality analysis (if recording exists) ---
        audio_quality = None
        if recording:
            mp3_path = os.path.join(RECORDINGS_DIR, recording)
            if os.path.exists(mp3_path):
                try:
                    audio_quality = analyze_recording_quality(mp3_path)
                except Exception as e:
                    logger.warning("Audio quality analysis failed for %s: %s", recording, e)

        if signal_ratio <= 0.0:
            # No signal detected at IQ level
            if audio_quality and audio_quality["audio_score"] >= 3:
                # IQ said no signal but audio analysis found decent content
                # (can happen with very weak but audible signals)
                score = audio_quality["audio_score"]
                stars_str = "★" * score + "☆" * (5 - score) + " (audio)"
            else:
                score = "nr"
                stars_str = "Segnale assente"
        else:
            iq_score = _signal_ratio_to_stars(signal_ratio)
            if audio_quality:
                audio_score = audio_quality["audio_score"]
                # v4: When audio analysis detects pure noise (0-1 stars),
                # trust it completely — IQ picks up carriers/interference
                # that sound like noise on the speaker.
                if audio_score <= 1:
                    score = audio_score
                    stars_str = "★" * score + "☆" * (5 - score) + \
                        " (audio:{} noise:{:.0%})".format(
                            audio_score, audio_quality["noise_level"])
                else:
                    # Blend: audio dominant for audible content
                    score = round(iq_score * 0.3 + audio_score * 0.7)
                    score = max(0, min(5, score))
                    stars_str = "★" * score + "☆" * (5 - score) + \
                        " (IQ:{} Audio:{} noise:{:.0%})".format(
                            iq_score, audio_score, audio_quality["noise_level"])
            else:
                score = iq_score
                stars_str = "★" * score + "☆" * (5 - score)

        self.ratings_db.add_rating(key, score, "automatico",
                                   recording=recording,
                                   audio_quality=audio_quality)

        entry = self.ratings_db.get_entry(key)
        avg = entry.get("avg_score") if entry else None
        avg_str = " (media {:.1f})".format(avg) if avg is not None else ""
        nr_count = entry.get("consecutive_nr", 0) if entry else 0

        # Store for live widget display
        total_ratings = len(entry.get("ratings", [])) if entry else 0
        self._last_quality = {
            "score": score,
            "station": event.get("description", "?"),
            "consecutive_nr": nr_count,
            "avg_score": avg,
            "total_ratings": total_ratings,
            "snr": round(avg_snr, 1),
        }

        logger.info("QUALITY: %s %s%s [nr x%d] (%d voti) SNR=%.1f",
                     event.get("description", "?"), stars_str, avg_str,
                     nr_count, total_ratings, avg_snr)

    # ====================================================================
    #  Main loop
    # ====================================================================

    def _orchestrator_loop(self):
        while self.running:
            try:
                if self.state == AutoModeState.AUTO:
                    self._handle_auto_mode()
                else:
                    time.sleep(1)
            except Exception as e:
                logger.error("Orchestrator loop error: %s", e, exc_info=True)
                time.sleep(5)

    def _handle_auto_mode(self):
        """Main decision method - called repeatedly by _orchestrator_loop.

        Priority:
          1. Calendar events on-air -> scan them all, 60 s each
          2. No events -> configured scan frequencies
        """
        try:
            # -- 1. Calendar events -------------------------------------
            active_events = self._get_active_schedule_events()

            if active_events:
                self._scan_events(active_events)
                return  # re-enter loop to check for new events

            # -- 2. Fallback scan frequencies ---------------------------
            if not self.frequencies:
                logger.warning("No scan frequencies and no events - sleeping 30s")
                time.sleep(30)
                return

            freq_config = self.frequencies[self.current_frequency_index]
            dwell = freq_config.get("dwell_time", 60)

            # Update current scan info for status API
            self._scanning_events = False
            self._current_scan_info = {
                "frequency": freq_config["frequency"],
                "mode": freq_config["mode"],
                "label": freq_config.get("label", "Scan"),
                "source": "scan",
                "scan_index": self.current_frequency_index,
                "scan_total": len(self.frequencies),
            }

            self._tune_one(
                freq_hz=freq_config["frequency"],
                mode=freq_config["mode"],
                label=freq_config.get("label", "Scan"),
                dwell_seconds=dwell,
                squelch=freq_config.get("squelch", 0.0),
                bandwidth=freq_config.get("bandwidth"),
                event=None,
            )

            if self.state == AutoModeState.AUTO:
                self.current_frequency_index = (
                    (self.current_frequency_index + 1) % len(self.frequencies)
                )

        except Exception as e:
            logger.error("Error in _handle_auto_mode: %s", e, exc_info=True)
            time.sleep(5)

    # -- event scanning --------------------------------------------------
    def _scan_events(self, events):
        """Scan through all active events, 60 s each, like a real scanner."""
        count = len(events)
        if self._event_scan_index >= count:
            self._event_scan_index = 0

        # Do ONE full pass through all events (or until state changes)
        scanned = 0
        while scanned < count and self.state == AutoModeState.AUTO:
            idx = (self._event_scan_index + scanned) % count
            ev = events[idx]
            freq_hz = int(float(ev.get("frequency_mhz", "0")) * 1_000_000)
            mode = ev.get("mode", "AM")
            label = ev.get("description", "?")
            bw_str = ev.get("bandwidth", "10")
            try:
                bw_hz = int(float(bw_str)) * 1000
            except (ValueError, TypeError):
                bw_hz = 10000

            logger.info("=====================================================")
            logger.info("EVENT %d/%d: %s", scanned + 1, count, label)
            logger.info("   %.3f MHz  %s  [%s]  %ds dwell",
                         freq_hz / 1e6, mode, ev.get("source", "?"),
                         EVENT_DWELL_SECONDS)
            logger.info("=====================================================")

            # Update current scan info for status API
            self._scanning_events = True
            self._current_scan_info = {
                "frequency": freq_hz,
                "mode": mode,
                "label": label,
                "source": ev.get("source", "?"),
                "event_index": scanned,
                "event_total": count,
                "time_utc": ev.get("time_utc", ""),
                "end_utc": ev.get("end_utc", ""),
            }

            self._tune_one(
                freq_hz=freq_hz,
                mode=mode,
                label=label,
                dwell_seconds=EVENT_DWELL_SECONDS,
                squelch=0.0,
                bandwidth=bw_hz,
                event=ev,
            )

            scanned += 1

            # After each dwell, re-check active events in case
            # new higher-priority events appeared or current ones ended
            if self.state == AutoModeState.AUTO:
                new_events = self._get_active_schedule_events()
                if not new_events:
                    logger.info("No more active events - returning to scan mode")
                    break
                if len(new_events) != count:
                    # Event list changed - restart scan with fresh list
                    break

        # Advance index for next call
        self._event_scan_index = (
            (self._event_scan_index + scanned) % max(count, 1)
        )

    # -- recording budget helpers ----------------------------------------
    def _rec_budget_ok(self, key: str) -> bool:
        """True if the station still has recording budget left in the current 2-hour window."""
        now_ts = time.monotonic()
        rec = self._rec_budgets.get(key)
        if rec is None:
            return True
        if now_ts - rec["window_start"] >= REC_WINDOW_SECONDS:
            # Window expired — reset
            del self._rec_budgets[key]
            return True
        remaining = REC_BUDGET_SECONDS - rec["used_sec"]
        if remaining <= 0:
            logger.info("REC BUDGET exhausted for '%s' (%.0f/%.0f s used in %.0f min window)",
                        key, rec["used_sec"], REC_BUDGET_SECONDS,
                        (now_ts - rec["window_start"]) / 60)
        return remaining > 0

    def _rec_budget_add(self, key: str, seconds: float):
        """Charge 'seconds' of recording time to the station's budget."""
        now_ts = time.monotonic()
        rec = self._rec_budgets.get(key)
        if rec is None or now_ts - rec["window_start"] >= REC_WINDOW_SECONDS:
            self._rec_budgets[key] = {"window_start": now_ts, "used_sec": seconds}
        else:
            rec["used_sec"] += seconds
            logger.debug("REC BUDGET '%s': %.0f/%.0f s used",
                         key, rec["used_sec"], REC_BUDGET_SECONDS)

    # -- tune + dwell + rate ---------------------------------------------
    def _tune_one(self, freq_hz, mode, label, dwell_seconds,
                  squelch=0.0, bandwidth=None, event=None):
        """Tune, dwell, measure signal quality, stop."""
        # -- tune --------------------------------------------------------
        if self.auto_tuner:
            ok = self.auto_tuner.tune_frequency(
                frequency=freq_hz, mode=mode,
                squelch=squelch, bandwidth=bandwidth,
            )
            if not ok:
                logger.error("Failed to tune to %s (%.3f MHz)",
                              label, freq_hz / 1e6)
                time.sleep(3)
                return

        time.sleep(self.config.get("transition_delay", 2))

        # -- start IQ power monitor for this dwell -----------------------
        source = None
        try:
            source = self.auto_tuner._get_source() if self.auto_tuner else None
            if source and source.isAvailable():
                self._iq_monitor.start(source)
        except Exception as e:
            logger.warning("Failed to start IQ monitor: %s", e)

        # -- set station info on SquelchRecorder for filename/metadata --
        try:
            from owrx.auto_squelch_recorder import SquelchRecorder
            rec = SquelchRecorder()
            station_name = label if event else None
            rec.set_station_info(station_name, freq_hz)
        except Exception:
            pass

        # -- start decoders / recorder -----------------------------------
        if self.decoder_manager and self.config.get("enable_decoders"):
            self.decoder_manager.start_session(freq_hz, mode)

        # -- check recording budget for this station ----------------------
        station_key = _station_key(event) if event else None
        rec_budget_allowed = (
            self.config.get("enable_recording")
            and station_key is not None
            and self._rec_budget_ok(station_key)
        ) or (self.config.get("enable_recording") and event is None)

        if self.auto_recorder and rec_budget_allowed:
            try:
                if hasattr(self.auto_recorder, "start_recording"):
                    self.auto_recorder.start_recording()
            except Exception as e:
                logger.error("Recorder start error: %s", e)
        elif self.auto_recorder and not rec_budget_allowed:
            logger.info("SKIP recording '%s' — budget exhausted for this 2h window", label)

        rec_start_ts = time.monotonic()

        # -- dwell + measure signal quality ------------------------------
        signal_ratio, samples, rec_filename = self._measure_signal_during_dwell(
            dwell_seconds,
            source=source, freq_hz=freq_hz, mode=mode,
            station_name=label if event else None,
        )

        # -- stop IQ monitor + decoders / recorder -----------------------
        self._iq_monitor.stop()

        if self.auto_recorder and rec_budget_allowed:
            try:
                if hasattr(self.auto_recorder, "stop_recording"):
                    self.auto_recorder.stop_recording()
            except Exception as e:
                logger.error("Recorder stop error: %s", e)
            # Charge actual dwell time to the station's budget
            if station_key:
                self._rec_budget_add(station_key, time.monotonic() - rec_start_ts)

        if self.decoder_manager:
            self.decoder_manager.stop_session()

        # -- rate the signal ---------------------------------------------
        if event is not None and samples > 0:
            self._rate_and_save(event, signal_ratio, recording=rec_filename)

    # ====================================================================
    #  Status / testing
    # ====================================================================

    def get_status(self):
        current_freq = None
        if self.state == AutoModeState.AUTO and self._current_scan_info:
            current_freq = dict(self._current_scan_info)  # copy
        elif (self.state == AutoModeState.AUTO
                and self.frequencies
                and self.current_frequency_index < len(self.frequencies)):
            current_freq = self.frequencies[self.current_frequency_index]
        return {
            "enabled": self.config["enabled"],
            "running": self.running,
            "state": self.state.value,
            "current_frequency": current_freq,
            "scanning_events": self._scanning_events,
            "last_quality": self._last_quality,
            "total_frequencies": len(self.frequencies),
            "event_dwell_seconds": EVENT_DWELL_SECONDS,
            "components": {
                "client_monitor": self.client_monitor is not None,
                "auto_tuner": self.auto_tuner is not None,
                "decoder_manager": self.decoder_manager is not None,
                "auto_recorder": self.auto_recorder is not None,
            },
        }

    def force_enter_auto_mode(self):
        if self.state == AutoModeState.MANUAL:
            self._enter_auto_mode()

    def force_exit_auto_mode(self):
        if self.state == AutoModeState.AUTO:
            self._exit_auto_mode()


# ========================================================================
#  Module init
# ========================================================================

def init_orchestrator():
    try:
        o = AutoModeOrchestrator.get_instance()
        o.start()
    except Exception as e:
        logger.error("Failed to init orchestrator: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    o = AutoModeOrchestrator.get_instance()
    o.start()
    print("\nOrchestrator Test")
    print("=" * 50)
    status = o.get_status()
    print(json.dumps(status, indent=2))

    # Test schedule scanning
    events = o._get_active_schedule_events()
    print("\nActive events right now: {}".format(len(events)))
    for i, ev in enumerate(events[:10]):
        key = _station_key(ev)
        enabled = o.ratings_db.is_enabled(key)
        state = "OK" if enabled else "DISABLED"
        print("  {}. {} {:>8s} MHz {:6s} {} {}".format(
              i+1, ev["time_utc"], ev["frequency_mhz"],
              ev.get("source","?"), state, ev["description"]))
    if len(events) > 10:
        print("  ... and {} more".format(len(events)-10))

    o.stop()
    print("\nTest completed!")
