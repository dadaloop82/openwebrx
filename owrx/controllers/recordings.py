"""
Recordings page controller - public standalone page with files iframe
Scheduler API controller - JSON status endpoint
"""

from owrx.controllers.template import TemplateController
from owrx.controllers import Controller
import json
import logging

logger = logging.getLogger(__name__)


class RecordingsPageController(TemplateController):
    """Public page that embeds /files in an iframe with status bar"""

    def indexAction(self):
        self.serve_template("recordings.html")


class SchedulerStatusController(Controller):
    """Public API endpoint for scheduler status"""

    def indexAction(self):
        try:
            from owrx.recording_scheduler import RecordingScheduler
            # Only return status if already initialized - don't lazy-create
            if RecordingScheduler.instance is None:
                self.send_response(
                    json.dumps({"running": False, "disabled": True,
                                "message": "RecordingScheduler disabled"},
                               default=str),
                    content_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return
            scheduler = RecordingScheduler.instance
            status = scheduler.get_status()

            self.send_response(
                json.dumps(status, default=str),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error getting scheduler status: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e), "running": False}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
