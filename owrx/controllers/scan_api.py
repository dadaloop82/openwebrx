"""
Scan Frequencies API Controller
Add/remove/list frequencies from the auto-mode scan list
"""

from owrx.controllers import Controller
import json
import os
import logging

logger = logging.getLogger(__name__)

CONFIG_FILE = None

def _get_config_file():
    global CONFIG_FILE
    if CONFIG_FILE is None:
        try:
            from owrx.config.core import CoreConfig
            CONFIG_FILE = os.path.join(CoreConfig().get_data_directory(), 'auto_mode_config.json')
        except Exception:
            CONFIG_FILE = '/var/lib/openwebrx/auto_mode_config.json'
    return CONFIG_FILE


def _load_config():
    path = _get_config_file()
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return {}


def _save_config(data):
    path = _get_config_file()
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


class ScanFrequenciesController(Controller):
    """API to manage scan frequency list"""

    def indexAction(self):
        """GET /api/scan/frequencies - list current scan frequencies"""
        try:
            config = _load_config()
            freqs = config.get('orchestrator', {}).get('frequencies', [])
            self.send_response(
                json.dumps({"frequencies": freqs}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error listing scan frequencies: %s", e)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json",
                code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )

    def add(self):
        """POST /api/scan/add - add a frequency to the scan list"""
        try:
            body = self.get_body()
            if not body:
                self.send_response(
                    json.dumps({"error": "Empty request body"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            data = json.loads(body.decode('utf-8'))
            freq_hz = int(data.get('frequency', 0))
            mode = data.get('mode', 'NFM').upper()
            bandwidth = int(data.get('bandwidth', 12500))
            squelch = float(data.get('squelch', 0.15))
            dwell_time = int(data.get('dwell_time', 90))
            label = data.get('label', '')

            if freq_hz < 100000:
                self.send_response(
                    json.dumps({"error": "Invalid frequency"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            # Auto-generate label if not provided
            if not label:
                freq_mhz = freq_hz / 1e6
                label = f"{freq_mhz:.3f} MHz {mode}"

            config = _load_config()
            if 'orchestrator' not in config:
                config['orchestrator'] = {'enabled': True, 'frequencies': [], 'cycle_mode': 'sequential',
                                          'enable_recording': True, 'enable_decoders': True, 'transition_delay': 2}

            freqs = config['orchestrator'].get('frequencies', [])

            # Check for duplicate (same freq within 1kHz tolerance)
            for existing in freqs:
                if abs(existing['frequency'] - freq_hz) < 1000 and existing['mode'] == mode:
                    self.send_response(
                        json.dumps({"error": "Frequency already in scan list",
                                    "label": existing.get('label', '')}),
                        content_type="application/json", code=409,
                        headers={"Access-Control-Allow-Origin": "*"}
                    )
                    return

            new_entry = {
                "frequency": freq_hz,
                "mode": mode,
                "squelch": squelch,
                "bandwidth": bandwidth,
                "dwell_time": dwell_time,
                "label": label
            }

            freqs.append(new_entry)
            config['orchestrator']['frequencies'] = freqs
            _save_config(config)

            # Hot-reload orchestrator frequencies
            try:
                from owrx.auto_mode_orchestrator import AutoModeOrchestrator
                orch = AutoModeOrchestrator.get_instance()
                orch.frequencies = freqs
                logger.info("Scan frequency added and hot-reloaded: %s", label)
            except Exception as e:
                logger.warning("Frequency saved but hot-reload failed: %s", e)

            self.send_response(
                json.dumps({"success": True, "entry": new_entry, "total": len(freqs)}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        except Exception as e:
            logger.error("Error adding scan frequency: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )

    def remove(self):
        """POST /api/scan/remove - remove a frequency from the scan list"""
        try:
            body = self.get_body()
            if not body:
                self.send_response(
                    json.dumps({"error": "Empty request body"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            data = json.loads(body.decode('utf-8'))
            freq_hz = int(data.get('frequency', 0))

            config = _load_config()
            freqs = config.get('orchestrator', {}).get('frequencies', [])
            original_count = len(freqs)

            freqs = [f for f in freqs if abs(f['frequency'] - freq_hz) >= 1000]

            if len(freqs) == original_count:
                self.send_response(
                    json.dumps({"error": "Frequency not found in scan list"}),
                    content_type="application/json", code=404,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            config['orchestrator']['frequencies'] = freqs
            _save_config(config)

            # Hot-reload
            try:
                from owrx.auto_mode_orchestrator import AutoModeOrchestrator
                orch = AutoModeOrchestrator.get_instance()
                orch.frequencies = freqs
            except Exception:
                pass

            self.send_response(
                json.dumps({"success": True, "total": len(freqs)}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        except Exception as e:
            logger.error("Error removing scan frequency: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )
