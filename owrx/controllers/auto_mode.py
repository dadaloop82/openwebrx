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
                compact[key] = {
                    "avg_score": entry.get("avg_score"),
                    "consecutive_nr": entry.get("consecutive_nr", 0),
                    "enabled": entry.get("enabled", True),
                    "total_ratings": len(entry.get("ratings", [])),
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
