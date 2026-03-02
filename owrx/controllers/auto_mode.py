"""
Auto Mode API Controller
Provides REST API for auto-mode status and ratings
"""

from owrx.controllers import Controller
import json
import logging

logger = logging.getLogger(__name__)


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
            for key, entry in all_ratings.items():
                ratings_list = entry.get("ratings", [])
                nr_count = sum(1 for r in ratings_list if r.get("score") == "nr")
                scored_count = sum(1 for r in ratings_list if isinstance(r.get("score"), (int, float)))
                # Last 5 positive detections (with timestamps)
                positives = [r for r in ratings_list if isinstance(r.get("score"), (int, float))]
                last_pos = [{"score": r["score"], "ts": r["ts"], "type": r.get("type", "?")} for r in positives[-5:]]
                compact[key] = {
                    "avg_score": entry.get("avg_score"),
                    "consecutive_nr": entry.get("consecutive_nr", 0),
                    "enabled": entry.get("enabled", True),
                    "total_ratings": len(ratings_list),
                    "nr_count": nr_count,
                    "scored_count": scored_count,
                    "last_positive": last_pos,
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
