from . import Controller
import json
import psutil


class SystemStatsController(Controller):
    """API endpoint returning real-time system stats (CPU, RAM, temperature)."""

    def indexAction(self):
        cpu_percent = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory()

        # CPU temperature: prefer coretemp, fallback to acpitz, then any
        cpu_temp = None
        mb_temp = None
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                if "coretemp" in temps and temps["coretemp"]:
                    cpu_temp = max(t.current for t in temps["coretemp"])
                if "acpitz" in temps and temps["acpitz"]:
                    mb_temp = temps["acpitz"][0].current
                # Fallback: if no coretemp, pick highest from any source
                if cpu_temp is None:
                    for entries in temps.values():
                        for t in entries:
                            if cpu_temp is None or t.current > cpu_temp:
                                cpu_temp = t.current
        except Exception:
            pass

        data = {
            "cpu_percent": round(cpu_percent, 1),
            "ram_percent": round(mem.percent, 1),
            "ram_used_mb": round(mem.used / (1024 * 1024)),
            "ram_total_mb": round(mem.total / (1024 * 1024)),
            "cpu_temp": round(cpu_temp, 1) if cpu_temp is not None else None,
            "mb_temp": round(mb_temp, 1) if mb_temp is not None else None,
        }

        self.send_response(json.dumps(data), content_type="application/json")
