"""
Auto Mode API Controller
Provides REST API for auto-mode status
"""

from owrx.controllers.template import WebpageController
import json
import logging

logger = logging.getLogger(__name__)


class AutoModeStatusController(WebpageController):
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
                content_type="application/json"
            )
