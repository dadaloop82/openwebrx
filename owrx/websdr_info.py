import urllib.request
import urllib.parse
import json
import re
import logging

logger = logging.getLogger(__name__)


class WebSdrInfo:
    """Fetches and parses info from a WebSDR server homepage."""

    @staticmethod
    def fetch(url: str) -> dict:
        url = url.rstrip("/") + "/"
        result = {
            "url": url,
            "reachable": False,
            "name": None,
            "users": None,
            "bands": [],
            "software": "WebSDR",
            "error": None,
        }
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OpenWebRX-WebSDR-Bridge/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            result["reachable"] = True
            # Users count
            m = re.search(r"(\d+)\s+users?", html, re.IGNORECASE)
            if m:
                result["users"] = int(m.group(1))
            # Title / name
            m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
            if m:
                result["name"] = m.group(1).strip()
            # Try to detect bands from band links or frequency info
            bands = re.findall(r"(\d+(?:\.\d+)?)\s*(?:kHz|MHz|KHz|MHZ)", html)
            seen = set()
            for b in bands[:20]:
                if b not in seen:
                    seen.add(b)
                    result["bands"].append(b)
            # Detect software version hint
            if "pa3fwm" in html.lower() or "websdr" in html.lower():
                result["software"] = "WebSDR (PA3FWM)"
            elif "openwebsdr" in html.lower():
                result["software"] = "OpenWebSDR"
        except Exception as e:
            result["error"] = str(e)
        return result
