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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum

logger = logging.getLogger(__name__)

# -- Paths ---------------------------------------------------------------
SCHEDULE_EVENTS_JSON = "/var/www/html/sdr-eibi-events.json"
RATINGS_DB_PATH = "/var/lib/openwebrx/signal_ratings.json"

# -- Tuning constants -----------------------------------------------------
EVENT_DWELL_SECONDS = 60                           # dwell per event in scan
SIGNAL_SAMPLE_INTERVAL = 3                         # seconds between samples
NR_DISABLE_THRESHOLD = 5                           # consecutive "nr" -> disable
MAX_EVENTS_PER_CYCLE = 20                          # max events per scan cycle
LONG_EVENT_THRESHOLD_MIN = 120                     # events >2h = "long-running"


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

    def add_rating(self, station_key, score, rating_type="automatico"):
        """Add a rating.  score = int 0-5 or string 'nr'."""
        with self._lock:
            entry = self._db.setdefault(station_key, {
                "ratings": [], "avg_score": None, "enabled": True,
                "consecutive_nr": 0,
            })
            entry["ratings"].append({
                "score": score,
                "type": rating_type,
                "ts": datetime.now(timezone.utc).isoformat(),
            })
            # Keep last 50 ratings max
            if len(entry["ratings"]) > 50:
                entry["ratings"] = entry["ratings"][-50:]

            # Update consecutive_nr
            if score == "nr":
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

        logger.info("AutoModeOrchestrator initialized")

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

                if end_str and ":" in end_str:
                    try:
                        eh, em = map(int, end_str.split(":"))
                        end = eh * 60 + em
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

    def _measure_signal_during_dwell(self, dwell_seconds):
        """Dwell for dwell_seconds while sampling the squelch-recorder state.

        Returns (signal_ratio, total_samples):
            signal_ratio = fraction of samples where audio signal was detected
            total_samples = number of samples taken
        """
        signal_count = 0
        total_samples = 0
        end_time = time.time() + dwell_seconds

        while time.time() < end_time and self.state == AutoModeState.AUTO:
            time.sleep(SIGNAL_SAMPLE_INTERVAL)
            total_samples += 1

            # Try reading SquelchRecorder singleton
            try:
                from owrx.auto_squelch_recorder import SquelchRecorder
                rec = SquelchRecorder()
                if rec.is_recording:
                    signal_count += 1
            except Exception:
                pass

        ratio = signal_count / max(total_samples, 1)
        return ratio, total_samples

    def _rate_and_save(self, event, signal_ratio):
        """Compute rating from signal_ratio and persist."""
        key = _station_key(event)
        if signal_ratio <= 0.0:
            score = "nr"
            stars_str = "Segnale assente"
        else:
            score = _signal_ratio_to_stars(signal_ratio)
            stars_str = "*" * score + "." * (5 - score)

        self.ratings_db.add_rating(key, score, "automatico")

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
        }

        logger.info("QUALITY: %s %s%s [nr x%d] (%d voti)",
                     event.get("description", "?"), stars_str, avg_str, nr_count, total_ratings)

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

        if self.auto_recorder and self.config.get("enable_recording"):
            try:
                if hasattr(self.auto_recorder, "start_recording"):
                    self.auto_recorder.start_recording()
            except Exception as e:
                logger.error("Recorder start error: %s", e)

        # -- dwell + measure signal quality ------------------------------
        signal_ratio, samples = self._measure_signal_during_dwell(dwell_seconds)

        # -- stop decoders / recorder ------------------------------------
        if self.auto_recorder and self.config.get("enable_recording"):
            try:
                if hasattr(self.auto_recorder, "stop_recording"):
                    self.auto_recorder.stop_recording()
            except Exception as e:
                logger.error("Recorder stop error: %s", e)

        if self.decoder_manager:
            self.decoder_manager.stop_session()

        # -- rate the signal ---------------------------------------------
        if event is not None and samples > 0:
            self._rate_and_save(event, signal_ratio)

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
