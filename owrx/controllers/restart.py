from owrx.controllers.admin import AuthorizationMixin
from owrx.controllers import Controller
import subprocess
import json
import threading
import time

import logging

logger = logging.getLogger(__name__)


class RestartController(AuthorizationMixin, Controller):
    def indexAction(self):
        if self.request.method != "POST":
            self.send_response(json.dumps({"error": "Method not allowed"}), code=405, content_type="application/json")
            return

        logger.info("Restart requested by user: %s", getattr(self.user, 'name', 'unknown'))

        self.send_response(json.dumps({"status": "restarting"}), code=200, content_type="application/json")

        # Restart after a short delay to allow the response to be sent
        def do_restart():
            time.sleep(1)
            try:
                subprocess.run(["systemctl", "restart", "openwebrx"], check=True)
            except Exception as e:
                logger.error("Failed to restart openwebrx: %s", e)

        threading.Thread(target=do_restart, daemon=True).start()
