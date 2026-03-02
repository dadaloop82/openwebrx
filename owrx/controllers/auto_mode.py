"""
Auto Mode API Controller
Provides REST API for auto-mode status and ratings
"""

from owrx.controllers import Controller
import json
import os
import re
import logging
import subprocess

logger = logging.getLogger(__name__)

RECORDINGS_DIR = "/var/lib/openwebrx/recordings"


class AutoModeStatusController(Controller):
    """Public API endpoint for auto-mode status"""
    
    def indexAction(self):
        try:
            from owrx.auto_mode_init import get_auto_mode_status
            status = get_auto_mode_status()
            
            self.send_response(
                json.dumps(status, default=str),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error getting auto-mode status: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e), "initialized": False}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )


class AutoModeRateController(Controller):
    """POST endpoint to submit a manual rating for a station.
    Body: {"key": "freq|desc", "score": 1-5}"""

    def postAction(self):
        try:
            data = json.loads(self.get_body().decode("utf-8"))
            key = data.get("key", "")
            score = data.get("score")
            if not key or score is None:
                self.send_response(
                    json.dumps({"error": "key and score required"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return
            score = int(score)
            if score < 1 or score > 5:
                self.send_response(
                    json.dumps({"error": "score must be 1-5"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return
            from owrx.auto_mode_orchestrator import AutoModeOrchestrator
            orch = AutoModeOrchestrator.get_instance()
            orch.ratings_db.add_rating(key, score, "manuale")
            logger.info("Manual rating: %s = %d stars", key, score)
            self.send_response(
                json.dumps({"ok": True, "key": key, "score": score}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error saving manual rating: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )

    def optionsAction(self):
        """Handle CORS preflight."""
        self.send_response(
            "", content_type="text/plain",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )


class AutoModeRatingsController(Controller):
    """Public API endpoint exposing the full signal ratings database
    so the schedule widget can update quality badges in real-time."""
    
    def indexAction(self):
        try:
            from owrx.auto_mode_orchestrator import AutoModeOrchestrator
            orch = AutoModeOrchestrator.get_instance()
            all_ratings = orch.ratings_db.get_all()
            
            # Build a compact summary for each station
            compact = {}
            # Scan recordings directory and build freq→files lookup
            rec_by_freq = _scan_recordings_dir()
            
            for key, entry in all_ratings.items():
                ratings_list = entry.get("ratings", [])
                nr_count = sum(1 for r in ratings_list if r.get("score") == "nr")
                scored_count = sum(1 for r in ratings_list if isinstance(r.get("score"), (int, float)))
                # Last 5 positive detections (with timestamps)
                positives = [r for r in ratings_list if isinstance(r.get("score"), (int, float))]
                last_pos = [{
                    "score": r["score"],
                    "ts": r["ts"],
                    "type": r.get("type", "?"),
                    "recording": r.get("recording"),
                } for r in positives[-5:]]
                
                # Also attach recordings from filesystem matched by frequency
                pipe_idx = key.find("|")
                freq_str = key[:pipe_idx] if pipe_idx >= 0 else key
                try:
                    freq_hz_key = str(int(round(float(freq_str) * 1e6)))
                except (ValueError, TypeError):
                    freq_hz_key = freq_str
                matched_recs = rec_by_freq.get(freq_hz_key, [])
                
                compact[key] = {
                    "avg_score": entry.get("avg_score"),
                    "consecutive_nr": entry.get("consecutive_nr", 0),
                    "enabled": entry.get("enabled", True),
                    "total_ratings": len(ratings_list),
                    "nr_count": nr_count,
                    "scored_count": scored_count,
                    "last_positive": last_pos,
                    "recordings": matched_recs,
                }
            
            self.send_response(
                json.dumps(compact, default=str),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error getting ratings: %s", e, exc_info=True)
            self.send_response(
                json.dumps({}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )


# ── helper: scan recordings directory ──────────────────────────────────

_FREQ_RE = re.compile(r'(\d+\.\d+)MHz')

def _scan_recordings_dir():
    """Return dict: freq_str → [{"filename","freq_mhz","date","time","size"}]
    where freq_str matches the key prefix used in the ratings DB (e.g. '5475000').
    """
    result = {}
    if not os.path.isdir(RECORDINGS_DIR):
        return result
    for fn in sorted(os.listdir(RECORDINGS_DIR)):
        if not fn.lower().endswith(('.mp3', '.wav')):
            continue
        m = _FREQ_RE.search(fn)
        if not m:
            continue
        freq_mhz = float(m.group(1))
        freq_hz_str = str(int(round(freq_mhz * 1e6)))
        # Parse date/time from filename: ...MHz_YYYYMMDD_HHMMSS.mp3
        parts = fn.split('_')
        date_str = ""
        time_str = ""
        for i, p in enumerate(parts):
            if 'MHz' in p:
                if i + 1 < len(parts):
                    date_str = parts[i + 1]
                if i + 2 < len(parts):
                    time_str = parts[i + 2].split('.')[0]
                break
        try:
            size = os.path.getsize(os.path.join(RECORDINGS_DIR, fn))
        except OSError:
            size = 0
        entry = {
            "filename": fn,
            "freq_mhz": freq_mhz,
            "date": date_str,
            "time": time_str,
            "size": size,
        }
        result.setdefault(freq_hz_str, []).append(entry)
    return result


class AutoModeRecordingsController(Controller):
    """GET /api/auto-mode/recordings — list all recordings grouped by frequency."""

    def indexAction(self):
        try:
            rec_by_freq = _scan_recordings_dir()
            self.send_response(
                json.dumps(rec_by_freq, default=str),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error listing recordings: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )
