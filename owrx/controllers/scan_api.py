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
            orch = config.get('orchestrator', {})
            freqs = orch.get('frequencies', [])

            # Normalize each entry to always have both frequency (Hz) and frequency_mhz
            normalized = []
            for f in freqs:
                entry = dict(f)
                if 'frequency' in entry and 'frequency_mhz' not in entry:
                    entry['frequency_mhz'] = round(entry['frequency'] / 1e6, 6)
                elif 'frequency_mhz' in entry and 'frequency' not in entry:
                    entry['frequency'] = int(float(entry['frequency_mhz']) * 1e6)
                # Normalize bandwidth: if > 1000, it's in Hz, convert display to kHz
                if 'bandwidth' in entry:
                    bw = entry['bandwidth']
                    if isinstance(bw, (int, float)) and bw > 1000:
                        entry['bandwidth_hz'] = int(bw)
                        entry['bandwidth'] = str(round(bw / 1000, 1))
                    elif isinstance(bw, str):
                        entry['bandwidth_hz'] = int(float(bw) * 1000) if bw else 12500
                # Round squelch to 2 decimal places
                if 'squelch' in entry and isinstance(entry['squelch'], float):
                    entry['squelch'] = round(entry['squelch'], 2)
                normalized.append(entry)

            self.send_response(
                json.dumps({
                    "frequencies": normalized,
                    "scan_enabled": orch.get('enabled', True),
                    "silence_timeout_seconds": orch.get('silence_timeout', 15)
                }),
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

    def postAction(self):
        """POST /api/scan/frequencies - replace full scan config (from frontend modal)"""
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
            config = _load_config()
            if 'orchestrator' not in config:
                config['orchestrator'] = {}

            if 'frequencies' in data:
                config['orchestrator']['frequencies'] = data['frequencies']
            if 'scan_enabled' in data:
                config['orchestrator']['enabled'] = data['scan_enabled']
            if 'silence_timeout_seconds' in data:
                config['orchestrator']['silence_timeout'] = data['silence_timeout_seconds']

            _save_config(config)

            try:
                from owrx.auto_mode_orchestrator import AutoModeOrchestrator
                orch = AutoModeOrchestrator.get_instance()
                orch.frequencies = config['orchestrator'].get('frequencies', [])
            except Exception:
                pass

            self.send_response(
                json.dumps({"success": True, "total": len(config['orchestrator'].get('frequencies', []))}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error in postAction: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )

    def deleteAction(self):
        """DELETE /api/scan/frequencies - remove frequency by index"""
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
            index = int(data.get('index', -1))

            config = _load_config()
            freqs = config.get('orchestrator', {}).get('frequencies', [])

            if index < 0 or index >= len(freqs):
                self.send_response(
                    json.dumps({"error": "Index out of range"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            removed = freqs.pop(index)
            config['orchestrator']['frequencies'] = freqs
            _save_config(config)

            try:
                from owrx.auto_mode_orchestrator import AutoModeOrchestrator
                orch = AutoModeOrchestrator.get_instance()
                orch.frequencies = freqs
            except Exception:
                pass

            self.send_response(
                json.dumps({"success": True, "removed": removed, "total": len(freqs)}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error("Error in deleteAction: %s", e, exc_info=True)
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
