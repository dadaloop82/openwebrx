from owrx.controllers.admin import Authentication
from owrx.controllers.template import WebpageController
from owrx.controllers.assets import AssetsController
from owrx.storage import Storage

import json
import re
import os
import logging
import subprocess
from datetime import datetime, timezone
from collections import OrderedDict

logger = logging.getLogger(__name__)


class FileController(AssetsController):
    def getFilePath(self, file):
        return Storage.getFilePath(file)

    def serve_file(self, file, content_type=None):
        # Add CORS header so the schedule widget (port 8080) can fetch audio for spectrograms
        self._cors_file = True
        super().serve_file(file, content_type)

    def send_response(self, content, code=200, **kwargs):
        headers = kwargs.pop("headers", None) or {}
        if getattr(self, "_cors_file", False):
            headers["Access-Control-Allow-Origin"] = "*"
        super().send_response(content, code=code, headers=headers, **kwargs)


class FilesController(WebpageController):
    # Path to signal ratings DB (maps freq|station_name → ratings)
    RATINGS_DB_PATH = "/var/lib/openwebrx/signal_ratings.json"

    def __init__(self, handler, request, options):
        self.authentication = Authentication()
        self.user  = self.authentication.getUser(request)
        self.isimg = re.compile(r'.*\.(png|bmp|gif|jpg)$')
        self.issnd = re.compile(r'.*\.(mp3|wav)$')
        super().__init__(handler, request, options)

    def isAuthorized(self):
        return self.user is not None and self.user.is_enabled() and not self.user.must_change_password

    def _local_to_utc_str(self, date_str, time_str):
        """Convert local date/time strings to UTC time string"""
        try:
            # date_str: DD/MM/YYYY, time_str: HH:MM:SS
            dt_str = f"{date_str} {time_str}"
            local_dt = datetime.strptime(dt_str, "%d/%m/%Y %H:%M:%S")
            local_dt = local_dt.astimezone()  # Make it timezone-aware (local)
            utc_dt = local_dt.astimezone(timezone.utc)
            return utc_dt.strftime("%H:%M:%S")
        except Exception:
            return ""

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

    def _build_station_lookup(self):
        """Build lookup tables from signal_ratings.json.

        Keys in the JSON are like "5.96|Radio Romania Int. (ROU)".
        Returns (freq_map, file_map, quality_map) where:
          freq_map  = {freq_str: station_name}
          file_map  = {filename: station_name}
          quality_map = {filename: {score, audio_score, noise_level}}
        """
        freq_map = {}    # "5.9600" → station name
        file_map = {}    # filename  → station name
        quality_map = {} # filename  → quality dict
        try:
            with open(self.RATINGS_DB_PATH, "r") as f:
                db = json.load(f)
            for key, entry in db.items():
                if "|" not in key:
                    continue
                freq_str, station_name = key.split("|", 1)
                station_name = station_name.strip()
                if not station_name:
                    continue
                # Normalize freq to 4-decimal string for matching
                try:
                    freq_norm = "%.4f" % float(freq_str)
                    freq_map[freq_norm] = station_name
                except (ValueError, TypeError):
                    pass
                # Index individual recording filenames with quality data
                for rating in entry.get("ratings", []):
                    rec = rating.get("recording")
                    if rec:
                        file_map[rec] = station_name
                        aq = rating.get("audio_quality")
                        quality_map[rec] = {
                            "score": rating.get("score"),
                            "audio_score": aq.get("audio_score") if aq else None,
                            "noise_level": aq.get("noise_level") if aq else None,
                        }
        except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
            logger.debug("_build_station_lookup: %s", e)
        return freq_map, file_map, quality_map

    def _lookup_station(self, filename, info, freq_map, file_map):
        """Find the station name for a recording."""
        if filename in file_map:
            return file_map[filename]
        if info.get('freq') is not None:
            freq_norm = "%.4f" % info['freq']
            if freq_norm in freq_map:
                return freq_map[freq_norm]
        return None

    def _quality_html(self, filename, quality_map):
        """Generate quality badge HTML for a recording."""
        q = quality_map.get(filename)
        if not q:
            return ""
        parts = []
        score = q.get("score")
        if isinstance(score, int) and score > 0:
            stars = '★' * score + '☆' * (5 - score)
            parts.append('<span class="q-stars">%s</span>' % stars)
        audio_score = q.get("audio_score")
        noise = q.get("noise_level")
        if audio_score is not None:
            noise_pct = int(noise * 100) if noise is not None else 0
            cls = 'q-good' if audio_score >= 4 else ('q-fair' if audio_score >= 3 else 'q-poor')
            parts.append('<span class="q-audio %s" title="Audio: %d/5, Rumore: %d%%">🔊%d/5 (📣%d%%)</span>' %
                         (cls, audio_score, noise_pct, audio_score, noise_pct))
        return ' '.join(parts)

    def _format_size(self, size_bytes):
        if size_bytes >= 1024 * 1024:
            return "%.1f MB" % (size_bytes / 1024 / 1024)
        elif size_bytes >= 1024:
            return "%.0f kB" % (size_bytes / 1024)
        elif size_bytes > 0:
            return "%d B" % size_bytes
        return ""

    DURATION_CACHE_PATH = "/var/lib/openwebrx/duration_cache.json"
    _duration_cache = None
    _duration_cache_dirty = False

    @classmethod
    def _load_duration_cache(cls):
        if cls._duration_cache is None:
            try:
                with open(cls.DURATION_CACHE_PATH, "r") as f:
                    cls._duration_cache = json.load(f)
            except Exception:
                cls._duration_cache = {}

    @classmethod
    def _save_duration_cache(cls):
        if cls._duration_cache_dirty:
            try:
                with open(cls.DURATION_CACHE_PATH, "w") as f:
                    json.dump(cls._duration_cache, f)
                cls._duration_cache_dirty = False
            except Exception:
                pass

    def _get_duration(self, filepath):
        self._load_duration_cache()
        key = os.path.basename(filepath)
        if key in self._duration_cache:
            return self._duration_cache[key]
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                capture_output=True, text=True, timeout=5
            )
            secs = float(result.stdout.strip())
            if secs < 1:
                dur = "<1s"
            elif secs < 60:
                dur = "%ds" % int(secs)
            elif secs < 3600:
                dur = "%dm%02ds" % (int(secs) // 60, int(secs) % 60)
            else:
                dur = "%dh%02dm" % (int(secs) // 3600, (int(secs) % 3600) // 60)
        except Exception:
            dur = ""
        self._duration_cache[key] = dur
        FilesController._duration_cache_dirty = True
        return dur

    def template_variables(self):
        files = Storage.getSharedInstance().getStoredFiles()

        # Build station-name + quality lookup from ratings DB
        freq_map, file_map, quality_map = self._build_station_lookup()

        # Build file info list and sort by timestamp descending
        file_entries = []
        for filename in files:
            filepath = Storage.getFilePath(filename)
            info = self._parse_filename(filename)
            is_audio = self.issnd.match(filename)
            is_image = self.isimg.match(filename)

            try:
                file_size = os.path.getsize(filepath)
                size_str = self._format_size(file_size)
            except Exception:
                file_size = 0
                size_str = ""

            # Skip small AUTO session .txt files with no useful decodings
            if filename.startswith('AUTO-') and filename.endswith('.txt') and file_size < 10240:
                try:
                    with open(filepath, 'r') as f:
                        content = f.read()
                    if 'Decodings: 0' in content or content.count('\n') < 10:
                        continue
                except Exception:
                    if file_size < 1024:
                        continue

            duration_str = self._get_duration(filepath) if is_audio else ""

            # Resolve station name and quality
            station_name = self._lookup_station(filename, info, freq_map, file_map)
            quality_html = self._quality_html(filename, quality_map)

            file_entries.append({
                'filename': filename,
                'filepath': filepath,
                'info': info,
                'is_audio': is_audio,
                'is_image': is_image,
                'size_str': size_str,
                'duration_str': duration_str,
                'station_name': station_name,
                'quality_html': quality_html,
            })

        # Sort descending by sort_key (newest first)
        file_entries.sort(key=lambda e: e['info']['sort_key'], reverse=True)
        self._save_duration_cache()

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

            group_id = 'grp_%d' % hash(group_label)
            MAX_VISIBLE = 10
            for idx, entry in enumerate(entries):
                filename = entry['filename']
                info = entry['info']
                is_audio = entry['is_audio']
                is_image = entry['is_image']
                size_str = entry['size_str']
                duration_str = entry['duration_str']

                is_auto = info.get('mode') == 'AUTO'
                is_rec = info.get('mode') == 'REC'
                is_ism = info.get('mode') == 'ISM'
                if is_auto:
                    icon = "🤖"
                elif is_rec:
                    icon = "🎙️"
                elif is_ism:
                    icon = "📡"
                elif is_audio:
                    icon = "🎵"
                elif is_image:
                    icon = "🖼️"
                else:
                    icon = "📄"
                card_class = "file-card"
                if is_auto:
                    card_class += " is-auto"
                elif is_rec:
                    card_class += " is-rec"
                elif is_ism:
                    card_class += " is-ism"
                if is_image:
                    card_class += " is-image"

                # Meta
                meta = []
                if info['freq']:
                    meta.append('<span class="freq">%.4f MHz</span>' % info['freq'])
                if info['mode']:
                    if info['mode'] == 'AUTO':
                        meta.append('<span class="mode-tag auto-mode-tag">🤖 AUTO</span>')
                    elif info['mode'] == 'REC':
                        meta.append('<span class="mode-tag rec-mode-tag">🎙️ REC</span>')
                    elif info['mode'] == 'ISM':
                        meta.append('<span class="mode-tag ism-mode-tag">📡 ISM</span>')
                    else:
                        meta.append('<span class="mode-tag">%s</span>' % info['mode'])
                if info['time']:
                    utc_str = self._local_to_utc_str(info['date'], info['time']) if info['date'] else ""
                    if utc_str:
                        meta.append('<span>%s <small style="opacity:0.6">(UTC %s)</small></span>' % (info['time'], utc_str))
                    else:
                        meta.append('<span>%s</span>' % info['time'])
                if duration_str:
                    meta.append('<span class="dur">%s</span>' % duration_str)
                if size_str:
                    meta.append('<span>%s</span>' % size_str)
                meta_html = ' '.join(meta)

                # Player
                player_html = ""
                if is_auto and filename.endswith('.txt'):
                    try:
                        txt_path = Storage.getFilePath(filename)
                        with open(txt_path, 'r') as txt_f:
                            txt_content = txt_f.read()
                        # Escape HTML
                        txt_content = txt_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        player_html = '<div class="auto-session-log"><pre>%s</pre></div>' % txt_content
                    except:
                        player_html = ""
                elif is_ism and filename.endswith('.txt'):
                    try:
                        txt_path = Storage.getFilePath(filename)
                        with open(txt_path, 'r') as txt_f:
                            txt_content = txt_f.read()
                        txt_content = txt_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                        player_html = '<div class="auto-session-log"><pre>%s</pre></div>' % txt_content
                    except:
                        player_html = ""
                elif is_audio:
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

                # Gemini AI button for audio files
                gemini_btn = ''
                if is_audio:
                    gemini_btn = '<button class="btn btn-ai gemini-rec-btn" data-file="%s" title="Chiedi a Gemini AI">🤖</button>' % filename

                buttons_html = (
                    '%s'
                    '<a class="btn btn-dl" href="/files/%s" download title="Download">⬇</a>'
                    '<button class="btn btn-del file-delete" data-name="%s" title="Elimina">✕</button>'
                ) % (gemini_btn, filename, filename)

                # Station name label (from EIBI/bookmark via ratings DB)
                station_html = ""
                if entry.get('station_name'):
                    safe_name = entry['station_name'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    station_html = '<span class="station-name">📻 %s</span>' % safe_name

                # Quality badge row
                quality_row = ''
                if entry.get('quality_html'):
                    quality_row = '<div class="card-quality">%s</div>' % entry['quality_html']

                rows += (
                    '<div class="%s"%s>'
                    '<div class="card-top">'
                    '<span class="file-icon">%s</span>'
                    '<span class="file-name">%s</span>'
                    '%s'
                    '<span class="file-meta">%s</span>'
                    '<span class="file-actions">%s</span>'
                    '</div>'
                    '%s'
                    '%s'
                    '</div>\n'
                ) % (card_class,
                     ' style="display:none" data-grp="%s"' % group_id if idx >= MAX_VISIBLE else '',
                     icon, filename, station_html, meta_html, buttons_html, quality_row, player_html)

            if len(entries) > MAX_VISIBLE:
                extra = len(entries) - MAX_VISIBLE
                rows += ('<button class="btn-load-more" onclick="'
                         "document.querySelectorAll('[data-grp=\"%s\"]').forEach(function(e){e.style.display=''});this.remove()"
                         '">\U0001F4C2 Carica altri %d...</button>\n') % (group_id, extra)

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

    def delete_all(self):
        try:
            files = Storage.getSharedInstance().getStoredFiles()
            deleted = 0
            for filename in files:
                try:
                    Storage.getSharedInstance().deleteFile(filename)
                    deleted += 1
                except Exception as e:
                    logger.debug("delete_all() skip %s: %s", filename, e)
            self.send_response(
                json.dumps({"deleted": deleted}),
                content_type="application/json", code=200
            )
        except Exception as e:
            logger.debug("delete_all(): " + str(e))
            self.send_response("{}", content_type="application/json", code=400)
