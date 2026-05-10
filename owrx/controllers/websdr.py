import json
import logging
from owrx.controllers import Controller
from owrx.controllers.admin import AuthorizationMixin
from owrx.config import Config
from owrx.websdr_info import WebSdrInfo

logger = logging.getLogger(__name__)


class WebSdrSetupController(Controller):
    """Serves the WebSDR setup/status page (GET /)  when no hardware SDR is active."""

    def indexAction(self):
        config = Config.get()
        websdr_url = config.get("websdr_url", "")
        if websdr_url:
            # Redirect directly into the embedded WebSDR page
            self.send_redirect("/websdr")
        else:
            html = self._render_setup_page()
            self.send_response(html, content_type="text/html")

    def _render_setup_page(self):
        return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>OpenWebRX – WebSDR Setup</title>
  <style>
    body { font-family: Arial, sans-serif; background: #1a1a2e; color: #eee; display: flex;
           align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
    .card { background: #16213e; border-radius: 12px; padding: 40px 50px; max-width: 500px;
            box-shadow: 0 4px 30px rgba(0,0,0,.5); text-align: center; }
    h1 { color: #e94560; margin-bottom: 8px; }
    p { color: #aaa; margin-bottom: 24px; }
    input[type=url] { width: 100%; padding: 12px; border-radius: 6px; border: 1px solid #444;
                      background: #0f3460; color: #fff; font-size: 15px; box-sizing: border-box; }
    button { margin-top: 16px; padding: 12px 32px; background: #e94560; color: #fff;
             border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }
    button:hover { background: #c73652; }
    .hint { font-size: 12px; color: #666; margin-top: 12px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>WebSDR Setup</h1>
    <p>Nessun SDR hardware configurato.<br>Inserisci l'URL di un WebSDR esterno per iniziare.</p>
    <form method="POST" action="/websdr/set-url">
      <input type="url" name="websdr_url" placeholder="http://websdr.ewi.utwente.nl:8901/"
             required autofocus>
      <button type="submit">Connetti</button>
    </form>
    <p class="hint">Puoi cambiare l'URL in qualsiasi momento da <a href="/settings/general" style="color:#e94560">Impostazioni &rarr; Generali &rarr; WebSDR Remote Source</a>.</p>
  </div>
</body>
</html>"""


class WebSdrPageController(Controller):
    """Mostra il WebSDR esterno in iframe + barra info."""

    def indexAction(self):
        config = Config.get()
        websdr_url = config.get("websdr_url", "").strip()
        if not websdr_url:
            self.send_redirect("/")
            return
        html = self._render_websdr_page(websdr_url)
        self.send_response(html, content_type="text/html")

    def _render_websdr_page(self, url):
        return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>OpenWebRX – WebSDR</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #111; display: flex; flex-direction: column; height: 100vh; }}
    #topbar {{ background: #1a1a2e; color: #eee; padding: 8px 16px; display: flex;
               align-items: center; gap: 16px; font-size: 13px; flex-shrink: 0; }}
    #topbar a {{ color: #e94560; text-decoration: none; }}
    #topbar a:hover {{ text-decoration: underline; }}
    #info {{ margin-left: auto; color: #aaa; }}
    iframe {{ flex: 1; border: none; width: 100%; background: #000; }}
  </style>
</head>
<body>
  <div id="topbar">
    <strong style="color:#e94560">OpenWebRX</strong>
    <span>&#x2192; WebSDR remoto:</span>
    <a href="{url}" target="_blank">{url}</a>
    <span id="info">Caricamento info...</span>
    <a href="/settings/general">&#9881; Impostazioni</a>
  </div>
  <iframe src="{url}" allowfullscreen></iframe>
  <script>
    fetch('/api/websdr/info')
      .then(r => r.json())
      .then(d => {{
        let s = '';
        if (d.name) s += d.name + ' | ';
        if (d.users !== null) s += d.users + ' utenti connessi | ';
        if (d.software) s += d.software;
        if (!d.reachable) s = 'Non raggiungibile: ' + (d.error || '');
        document.getElementById('info').textContent = s;
      }})
      .catch(() => {{ document.getElementById('info').textContent = 'Info non disponibili'; }});
  </script>
</body>
</html>""".format(url=url)


class WebSdrSetUrlController(Controller):
    """Salva l'URL WebSDR dalla form di setup (POST /websdr/set-url)."""

    def indexAction(self):
        body = self.get_body(max_size=2048)
        if body:
            from urllib.parse import parse_qs
            params = parse_qs(body.decode("utf-8", errors="replace"))
            url = params.get("websdr_url", [""])[0].strip()
            if url:
                config = Config.get()
                config["websdr_url"] = url
        self.send_redirect("/websdr")


class WebSdrInfoApiController(Controller):
    """API JSON: restituisce info sul WebSDR configurato (GET /api/websdr/info)."""

    def indexAction(self):
        config = Config.get()
        url = config.get("websdr_url", "").strip()
        if not url:
            result = {"reachable": False, "error": "No WebSDR URL configured"}
        else:
            result = WebSdrInfo.fetch(url)
        self.send_response(
            json.dumps(result),
            content_type="application/json",
        )
