from owrx.controllers.admin import Authentication
from owrx.controllers.template import WebpageController
from owrx.controllers.assets import AssetsController
from owrx.storage import Storage

import json
import re
import os
import logging
import subprocess
from datetime import datetime
from collections import OrderedDict

logger = logging.getLogger(__name__)


class FileController(AssetsController):
    def getFilePath(self, file):
        return Storage.getFilePath(file)


class FilesController(WebpageController):
    def __init__(self, handler, request, options):
        self.authentication = Authentication()
        self.user  = self.authentication.getUser(request)
        self.isimg = re.compile(r'.*\.(png|bmp|gif|jpg)$')
        self.issnd = re.compile(r'.*\.(mp3|wav)$')
        super().__init__(handler, request, options)

    def isAuthorized(self):
        return self.user is not None and self.user.is_enabled() and not self.user.must_change_password

    def _parse_filename(self, filename):
        info = {
            'freq': None, 'date': None, 'time': None, 'mode': None,
            'type': 'file', 'sort_key': '', 'group_key': '',
        }
        # Pattern: FREQ_MHz_DATE_TIME.ext
        m = re.match(r'([\d.]+)MHz_(\d{8})_(\d{6})\.(mp3|wav)', filename)
        if m:
            info['freq'] = float(m.group(1))
            ds = m.group(2)
            ts = m.group(3)
            info['date'] = f"{ds[6:8]}/{ds[4:6]}/{ds[0:4]}"
            info['time'] = f"{ts[0:2]}:{ts[2:4]}:{ts[4:6]}"
            info['type'] = 'recording'
            info['sort_key'] = ds + ts
            info['group_key'] = f"{ds[6:8]}/{ds[4:6]}/{ds[0:4]} - Ore {ts[0:2]}:00"
            return info

        m = re.match(r'REC_(\d{8})_(\d{6})\.(mp3|wav)', filename)
        if m:
            ds = m.group(1)
            ts = m.group(2)
            info['date'] = f"{ds[6:8]}/{ds[4:6]}/{ds[0:4]}"
            info['time'] = f"{ts[0:2]}:{ts[2:4]}:{ts[4:6]}"
            info['type'] = 'recording'
            info['sort_key'] = ds + ts
            info['group_key'] = f"{ds[6:8]}/{ds[4:6]}/{ds[0:4]} - Ore {ts[0:2]}:00"
            return info

        m = re.match(r'([A-Z0-9]+)-(\d{6})-(\d{6})(?:-(\d+))?(?:-(\d+))?\.(\w+)', filename)
        if m:
            mode_str = m.group(1)
            ds = m.group(2)
            ts = m.group(3)
            freq_khz = m.group(4)
            info['mode'] = mode_str
            info['date'] = f"{ds[4:6]}/{ds[2:4]}/20{ds[0:2]}"
            info['time'] = f"{ts[0:2]}:{ts[2:4]}:{ts[4:6]}"
            if freq_khz:
                info['freq'] = int(freq_khz) / 1000.0
            info['type'] = 'decode'
            info['sort_key'] = '20' + ds + ts
            info['group_key'] = f"{ds[4:6]}/{ds[2:4]}/20{ds[0:2]} - Ore {ts[0:2]}:00"
            return info

        return info

    def _format_size(self, size_bytes):
        if size_bytes >= 1024 * 1024:
            return "%.1f MB" % (size_bytes / 1024 / 1024)
        elif size_bytes >= 1024:
            return "%.0f kB" % (size_bytes / 1024)
        elif size_bytes > 0:
            return "%d B" % size_bytes
        return ""

    def _get_duration(self, filepath):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                capture_output=True, text=True, timeout=5
            )
            secs = float(result.stdout.strip())
            if secs < 1:
                return "<1s"
            elif secs < 60:
                return "%ds" % int(secs)
            elif secs < 3600:
                return "%dm%02ds" % (int(secs) // 60, int(secs) % 60)
            else:
                return "%dh%02dm" % (int(secs) // 3600, (int(secs) % 3600) // 60)
        except Exception:
            return ""

    def template_variables(self):
        files = Storage.getSharedInstance().getStoredFiles()

        # Build file info list and sort by timestamp descending
        file_entries = []
        for filename in files:
            filepath = Storage.getFilePath(filename)
            info = self._parse_filename(filename)
            is_audio = self.issnd.match(filename)
            is_image = self.isimg.match(filename)

            try:
                size_str = self._format_size(os.path.getsize(filepath))
            except Exception:
                size_str = ""

            duration_str = self._get_duration(filepath) if is_audio else ""

            file_entries.append({
                'filename': filename,
                'filepath': filepath,
                'info': info,
                'is_audio': is_audio,
                'is_image': is_image,
                'size_str': size_str,
                'duration_str': duration_str,
            })

        # Sort descending by sort_key (newest first)
        file_entries.sort(key=lambda e: e['info']['sort_key'], reverse=True)

        # Group by day+hour
        groups = OrderedDict()
        for entry in file_entries:
            gk = entry['info']['group_key'] or 'Altri file'
            if gk not in groups:
                groups[gk] = []
            groups[gk].append(entry)

        # Build HTML
        rows = ""
        for group_label, entries in groups.items():
            rows += '<div class="file-group">\n'
            rows += '<div class="group-header" onclick="$(this).next().slideToggle(150);$(this).toggleClass(\'collapsed\')">'
            rows += '<span class="group-arrow">▼</span> 📁 %s <span class="group-count">(%d)</span></div>\n' % (group_label, len(entries))
            rows += '<div class="group-body">\n'

            for entry in entries:
                filename = entry['filename']
                info = entry['info']
                is_audio = entry['is_audio']
                is_image = entry['is_image']
                size_str = entry['size_str']
                duration_str = entry['duration_str']

                icon = "🎵" if is_audio else ("🖼️" if is_image else "📄")
                card_class = "file-card" + (" is-image" if is_image else "")

                # Meta
                meta = []
                if info['freq']:
                    meta.append('<span class="freq">%.4f MHz</span>' % info['freq'])
                if info['mode']:
                    meta.append('<span class="mode-tag">%s</span>' % info['mode'])
                if info['time']:
                    meta.append('<span>%s</span>' % info['time'])
                if duration_str:
                    meta.append('<span class="dur">%s</span>' % duration_str)
                if size_str:
                    meta.append('<span>%s</span>' % size_str)
                meta_html = ' '.join(meta)

                # Player
                player_html = ""
                if is_audio:
                    player_html = (
                        '<div class="card-player">'
                        '<div class="viz-wrap">'
                        '<canvas class="spectrogram-canvas"></canvas>'
                        '<canvas class="waveform-canvas"></canvas>'
                        '<div class="waveform-overlay"></div>'
                        '</div>'
                        '<audio controls preload="metadata" src="/files/%s"></audio>'
                        '</div>'
                    ) % filename
                elif is_image:
                    player_html = '<a href="/files/%s" target="_blank"><img class="file-img-preview" src="/files/%s" alt="%s"/></a>' % (filename, filename, filename)

                buttons_html = (
                    '<a class="btn btn-dl" href="/files/%s" download title="Download">⬇</a>'
                    '<button class="btn btn-del file-delete" data-name="%s" title="Elimina">✕</button>'
                ) % (filename, filename)

                rows += (
                    '<div class="%s">'
                    '<div class="card-top">'
                    '<span class="file-icon">%s</span>'
                    '<span class="file-name">%s</span>'
                    '<span class="file-meta">%s</span>'
                    '<span class="file-actions">%s</span>'
                    '</div>'
                    '%s'
                    '</div>\n'
                ) % (card_class, icon, filename, meta_html, buttons_html, player_html)

            rows += '</div></div>\n'

        variables = super().template_variables()
        variables["rows"] = rows
        return variables

    def indexAction(self):
        self.serve_template("files.html", **self.template_variables())

    def delete(self):
        try:
            data = json.loads(self.get_body().decode("utf-8"))
            file = data["name"].strip() if "name" in data else ""
            if len(file) > 0:
                Storage.getSharedInstance().deleteFile(file)
            self.send_response("{}", content_type="application/json", code=200)
        except Exception as e:
            logger.debug("delete(): " + str(e))
            self.send_response("{}", content_type="application/json", code=400)
