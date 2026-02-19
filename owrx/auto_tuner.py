"""
Automatic Tuner for OpenWebRX
Controls SDR source frequency directly via SdrService.
Used by auto-mode to switch between frequencies automatically.
"""

import logging
import threading
from typing import Optional, Dict, Any

from owrx.source import SdrSourceEventClient, SdrClientClass, SdrSourceState

logger = logging.getLogger(__name__)


class AutoTuner(SdrSourceEventClient):
    """Controls the SDR source to automatically tune frequencies.

    Implements SdrSourceEventClient so it can register as a BACKGROUND
    client on the SDR source, which causes the source process
    (rtl_connector, etc.) to start and stay running while auto-mode
    is active.
    """

    instance = None
    lock = threading.Lock()

    @staticmethod
    def get_instance():
        with AutoTuner.lock:
            if AutoTuner.instance is None:
                AutoTuner.instance = AutoTuner()
        return AutoTuner.instance

    def __init__(self):
        self.current_frequency = None
        self.current_mode = None
        self.current_squelch = None
        self.current_bandwidth = None
        self.tuner_lock = threading.Lock()
        self.is_auto_mode = False
        # Pre-tune state to restore when exiting auto mode
        self._saved_profile = None
        self._saved_source_id = None
        # Track which source we registered on as BACKGROUND client
        self._registered_source = None
        logger.info("AutoTuner initialized")

    # --- SdrSourceEventClient interface ---
    def getClientClass(self) -> SdrClientClass:
        return SdrClientClass.BACKGROUND

    def onStateChange(self, state: SdrSourceState):
        if state == SdrSourceState.STOPPING:
            logger.warning("SDR source stopping while AutoTuner is active")

    def onShutdown(self):
        logger.warning("SDR source shut down while AutoTuner is active")
        self._registered_source = None

    def _register_on_source(self, source):
        """Register as BACKGROUND client so the source starts."""
        if self._registered_source is source:
            # Already registered, but make sure source is started
            if not source.isAvailable():
                source.start()
            return
        self._unregister_from_source()
        source.addClient(self)
        self._registered_source = source
        logger.info("📡 Registered as BACKGROUND client on source '%s'",
                     source.getName())

    def _unregister_from_source(self):
        """Remove ourselves from the source so it can stop."""
        if self._registered_source is not None:
            try:
                self._registered_source.removeClient(self)
                logger.info("📡 Unregistered from source '%s'",
                             self._registered_source.getName())
            except Exception as e:
                logger.warning("Error unregistering from source: %s", e)
            self._registered_source = None

    # ------------------------------------------------------------------
    # Helpers to talk to SdrService / SdrSource
    # ------------------------------------------------------------------
    def _get_source(self):
        """Return the first available SdrSource, or None."""
        try:
            from owrx.sdr import SdrService
            source = SdrService.getFirstSource()
            if source is None:
                logger.warning("No active SDR source available")
            return source
        except Exception as e:
            logger.error("Error obtaining SDR source: %s", e)
            return None

    def _find_matching_profile(self, source, frequency, mode=None):
        """
        Look through a source's profiles for one whose band covers
        *frequency*.  If *mode* is given, prefer a profile whose
        modulation matches.
        Returns the profile_id string, or None.
        """
        best = None
        for p_id, profile in source.getProfiles().items():
            if "center_freq" not in profile or "samp_rate" not in profile:
                continue
            cf = profile["center_freq"]
            sr = profile["samp_rate"]
            half = sr / 2
            if cf - half <= frequency <= cf + half:
                # This profile covers the frequency
                mod = profile["modulation"].upper() if "modulation" in profile else ""
                if mode and mod == mode.upper():
                    return p_id          # exact match on mode → use it
                if best is None:
                    best = p_id          # first match
        return best

    def _find_any_source_and_profile(self, frequency, mode=None):
        """
        Search ALL active SDR sources for one that can tune to *frequency*.
        Returns (source, profile_id) or (None, None).
        """
        try:
            from owrx.sdr import SdrService
            sources = SdrService.getActiveSources()
            if not sources:
                return None, None
            for s_id, source in sources.items():
                p_id = self._find_matching_profile(source, frequency, mode)
                if p_id is not None:
                    return source, p_id
            # Fallback: just use first source, no profile switch
            return SdrService.getFirstSource(), None
        except Exception as e:
            logger.error("Error searching sources: %s", e)
            return None, None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_receiver_status(self) -> Dict[str, Any]:
        """Get current SDR source status."""
        source = self._get_source()
        if source is None:
            return {"connected": False, "error": "No SDR source available"}
        try:
            props = source.getProps()
            return {
                "connected": True,
                "source_id": source.getId(),
                "source_name": source.getName(),
                "center_freq": props.get("center_freq"),
                "samp_rate":   props.get("samp_rate"),
                "profile_id":  source.getProfileId(),
            }
        except Exception as e:
            logger.error("Error getting receiver status: %s", e)
            return {"connected": True, "error": str(e)}

    def tune_frequency(self, frequency: int, mode: str = None,
                       squelch: float = None, bandwidth: int = None) -> bool:
        """
        Tune the SDR source to *frequency* (Hz).

        1. Find a source/profile whose band covers the frequency.
        2. Register as BACKGROUND client (starts the source if needed).
        3. Activate that profile (if needed).
        4. Move the centre frequency to the target.

        Returns True on success.
        """
        with self.tuner_lock:
            try:
                logger.info("🎯 Tuning to %.3f MHz (mode=%s, squelch=%s, bw=%s)",
                            frequency / 1e6, mode, squelch, bandwidth)

                source, p_id = self._find_any_source_and_profile(frequency, mode)
                if source is None:
                    logger.error("Cannot tune: no SDR source available")
                    return False

                # Register as BACKGROUND client → starts the source process
                if self.is_auto_mode:
                    self._register_on_source(source)

                    # Wait for the source to become available (up to 5s)
                    import time
                    for i in range(50):
                        if source.isAvailable():
                            break
                        time.sleep(0.1)
                    if not source.isAvailable():
                        logger.warning("Source not available after 5s, "
                                       "proceeding anyway")

                # Activate the matching profile if we found one
                if p_id is not None:
                    current_profile = source.getProfileId()
                    if p_id != current_profile:
                        logger.info("   Switching to profile '%s'", p_id)
                        source.activateProfile(p_id)

                # Set centre frequency
                source.setCenterFreq(frequency)
                self.current_frequency = frequency

                if mode:
                    self.current_mode = mode
                if squelch is not None:
                    self.current_squelch = squelch
                if bandwidth is not None:
                    self.current_bandwidth = bandwidth

                logger.info("✅ Successfully tuned to %.3f MHz on source '%s'",
                            frequency / 1e6, source.getName())
                return True

            except Exception as e:
                logger.error("Error tuning frequency: %s", e, exc_info=True)
                return False

    def get_current_settings(self) -> Dict[str, Any]:
        """Get current tuner settings."""
        return {
            "frequency":    self.current_frequency,
            "mode":         self.current_mode,
            "squelch":      self.current_squelch,
            "bandwidth":    self.current_bandwidth,
            "is_auto_mode": self.is_auto_mode,
        }

    # ------------------------------------------------------------------
    # Auto / manual mode transitions
    # ------------------------------------------------------------------
    def enter_auto_mode(self):
        """Save current state and mark auto mode active."""
        # Save current source/profile so we can restore later
        try:
            source = self._get_source()
            if source:
                self._saved_source_id = source.getId()
                self._saved_profile = source.getProfileId()
        except Exception:
            pass
        self.is_auto_mode = True
        logger.info("═══════════════════════════════════════════════════")
        logger.info("🤖 AUTO MODE ACTIVATED")
        logger.info("═══════════════════════════════════════════════════")

    def exit_auto_mode(self):
        """Restore previous state and leave auto mode."""
        self.is_auto_mode = False
        # Unregister from the SDR source
        self._unregister_from_source()
        # Restore previous profile if we have one
        if self._saved_source_id and self._saved_profile:
            try:
                from owrx.sdr import SdrService
                source = SdrService.getSource(self._saved_source_id)
                if source:
                    source.activateProfile(self._saved_profile)
                    logger.info("Restored profile '%s' on source '%s'",
                                self._saved_profile, self._saved_source_id)
            except Exception as e:
                logger.warning("Could not restore previous profile: %s", e)
        self._saved_source_id = None
        self._saved_profile = None
        logger.info("═══════════════════════════════════════════════════")
        logger.info("👤 MANUAL MODE — User control restored")
        logger.info("═══════════════════════════════════════════════════")

    def restore_settings(self, settings: Dict[str, Any]) -> bool:
        """Restore previous settings (frequency, mode, etc.)."""
        try:
            if settings.get("frequency"):
                return self.tune_frequency(
                    frequency=settings["frequency"],
                    mode=settings.get("mode"),
                    squelch=settings.get("squelch"),
                    bandwidth=settings.get("bandwidth"),
                )
            return True
        except Exception as e:
            logger.error("Error restoring settings: %s", e)
            return False


def init_auto_tuner(receiver=None):
    """Initialize the auto tuner (receiver argument kept for compat)."""
    try:
        AutoTuner.get_instance()
    except Exception as e:
        logger.error("Failed to initialize AutoTuner: %s", e)
