"""
Gemini AI Radio Analysis Controller
Sends frequency info + optional audio sample to Google Gemini for analysis
"""

from owrx.controllers import Controller
import json
import os
import logging
import base64
import struct
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

def _get_gemini_url():
    key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")
    model = GEMINI_MODEL or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# Audio capture buffer (receives ADPCM-compressed audio from the main demodulated output)
_audio_buffer = bytearray()
_audio_lock = threading.Lock()
_audio_capturing = False
_capture_start_time = 0
CAPTURE_DURATION = 5  # seconds
SAMPLE_RATE = 12000  # output_rate of the DSP chain
MAX_PCM_BYTES = SAMPLE_RATE * 2 * CAPTURE_DURATION  # 16-bit mono PCM target

# IMA-ADPCM decoder state
_adpcm_step_index = 0
_adpcm_predictor = 0
_adpcm_phase = 0  # 0=searching sync, 1=reading state, 2=decoding
_adpcm_sync_pos = 0
_adpcm_sync_buf = bytearray(4)
_adpcm_sync_buf_idx = 0
_adpcm_sync_counter = 0

_IMA_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
_IMA_STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
]
_SYNC_WORD = b'SYNC'


def _decode_nibble(nibble):
    """Decode a single IMA-ADPCM nibble, updating global state"""
    global _adpcm_step_index, _adpcm_predictor
    _adpcm_step_index += _IMA_INDEX_TABLE[nibble]
    _adpcm_step_index = max(0, min(_adpcm_step_index, 88))
    step = _IMA_STEP_TABLE[_adpcm_step_index]
    diff = step >> 3
    if nibble & 1:
        diff += step >> 2
    if nibble & 2:
        diff += step >> 1
    if nibble & 4:
        diff += step
    if nibble & 8:
        diff = -diff
    _adpcm_predictor += diff
    _adpcm_predictor = max(-32768, min(_adpcm_predictor, 32767))
    return _adpcm_predictor


def _decode_adpcm_with_sync(data: bytes) -> bytes:
    """Decode IMA-ADPCM data with sync markers to 16-bit PCM.
    Returns raw int16 LE bytes."""
    global _adpcm_phase, _adpcm_sync_pos, _adpcm_sync_buf, _adpcm_sync_buf_idx
    global _adpcm_step_index, _adpcm_predictor, _adpcm_sync_counter

    out = bytearray()
    for byte_val in data:
        if _adpcm_phase == 0:
            # Searching for SYNC word
            if byte_val == _SYNC_WORD[_adpcm_sync_pos]:
                _adpcm_sync_pos += 1
            else:
                _adpcm_sync_pos = 0
            if _adpcm_sync_pos == 4:
                _adpcm_sync_buf_idx = 0
                _adpcm_phase = 1
        elif _adpcm_phase == 1:
            # Reading 4 bytes of codec state (stepIndex int16 LE + predictor int16 LE)
            _adpcm_sync_buf[_adpcm_sync_buf_idx] = byte_val
            _adpcm_sync_buf_idx += 1
            if _adpcm_sync_buf_idx == 4:
                _adpcm_step_index = struct.unpack_from('<h', _adpcm_sync_buf, 0)[0]
                _adpcm_predictor = struct.unpack_from('<h', _adpcm_sync_buf, 2)[0]
                _adpcm_sync_counter = 1000
                _adpcm_phase = 2
        elif _adpcm_phase == 2:
            # Decode audio nibbles (low nibble first, then high)
            sample_lo = _decode_nibble(byte_val & 0x0F)
            out.extend(struct.pack('<h', sample_lo))
            sample_hi = _decode_nibble((byte_val >> 4) & 0x0F)
            out.extend(struct.pack('<h', sample_hi))
            _adpcm_sync_counter -= 1
            if _adpcm_sync_counter <= 0:
                _adpcm_sync_pos = 0
                _adpcm_phase = 0
    return bytes(out)


def start_audio_capture():
    """Start capturing audio from the main demodulated output"""
    global _audio_buffer, _audio_capturing, _capture_start_time
    global _adpcm_phase, _adpcm_sync_pos, _adpcm_step_index, _adpcm_predictor, _adpcm_sync_counter
    with _audio_lock:
        _audio_buffer = bytearray()
        _audio_capturing = True
        _capture_start_time = time.time()
        # Reset ADPCM decoder state
        _adpcm_phase = 0
        _adpcm_sync_pos = 0
        _adpcm_step_index = 0
        _adpcm_predictor = 0
        _adpcm_sync_counter = 0
    logger.info("Gemini audio capture started (max %ds)", CAPTURE_DURATION)


def feed_audio_capture(data: bytes, is_adpcm: bool = True):
    """Feed audio data into capture buffer.
    Called from write_dsp_data() with ADPCM-compressed audio from the main demodulated output.
    The first byte (0x02 type tag) must already be stripped by the caller."""
    global _audio_capturing
    if not _audio_capturing:
        return
    with _audio_lock:
        if not _audio_capturing:
            return
        if time.time() - _capture_start_time > CAPTURE_DURATION:
            _audio_capturing = False
            return
        # Decode ADPCM to PCM
        if is_adpcm:
            pcm = _decode_adpcm_with_sync(data)
        else:
            pcm = data
        remaining = MAX_PCM_BYTES - len(_audio_buffer)
        if remaining > 0:
            _audio_buffer.extend(pcm[:remaining])
        if len(_audio_buffer) >= MAX_PCM_BYTES:
            _audio_capturing = False


def stop_and_get_audio():
    """Stop capture and return the audio as WAV bytes"""
    global _audio_capturing
    with _audio_lock:
        _audio_capturing = False
        pcm_data = bytes(_audio_buffer)
        _audio_buffer.clear()

    if len(pcm_data) < SAMPLE_RATE * 2 // 5:  # less than 0.2s
        logger.warning("Gemini audio capture too short: %d bytes (%.1fs)",
                       len(pcm_data), len(pcm_data) / (SAMPLE_RATE * 2))
        return None

    logger.info("Gemini audio capture: %d bytes (%.1fs)",
                len(pcm_data), len(pcm_data) / (SAMPLE_RATE * 2))

    # Build WAV header
    num_samples = len(pcm_data) // 2
    data_size = num_samples * 2
    wav = bytearray()
    wav.extend(b'RIFF')
    wav.extend(struct.pack('<I', 36 + data_size))
    wav.extend(b'WAVE')
    wav.extend(b'fmt ')
    wav.extend(struct.pack('<I', 16))       # chunk size
    wav.extend(struct.pack('<H', 1))        # PCM
    wav.extend(struct.pack('<H', 1))        # mono
    wav.extend(struct.pack('<I', SAMPLE_RATE))
    wav.extend(struct.pack('<I', SAMPLE_RATE * 2))  # byte rate
    wav.extend(struct.pack('<H', 2))        # block align
    wav.extend(struct.pack('<H', 16))       # bits per sample
    wav.extend(b'data')
    wav.extend(struct.pack('<I', data_size))
    wav.extend(pcm_data)
    return bytes(wav)


def _call_gemini(prompt: str, audio_wav: bytes = None) -> str:
    """Call Google Gemini API with text prompt and optional audio"""
    import urllib.request
    import urllib.error

    api_key = GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return "Errore: GEMINI_API_KEY non configurata. Impostare la variabile d'ambiente GEMINI_API_KEY."

    gemini_url = _get_gemini_url()

    parts = [{"text": prompt}]

    if audio_wav is not None:
        audio_b64 = base64.b64encode(audio_wav).decode('ascii')
        parts.append({
            "inline_data": {
                "mime_type": "audio/wav",
                "data": audio_b64
            }
        })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048
        }
    }

    data = json.dumps(payload).encode('utf-8')

    max_retries = 3
    base_delay = 2  # seconds
    last_error = None

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            gemini_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                candidates = result.get('candidates', [])
                if candidates:
                    content = candidates[0].get('content', {})
                    text_parts = [p.get('text', '') for p in content.get('parts', [])]
                    return '\n'.join(text_parts)
                return "No response from Gemini"
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            last_error = f"Gemini API error {e.code}: {body[:200]}"
            if e.code == 429 and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning("Gemini API rate limited (429), retry %d/%d in %ds",
                               attempt + 1, max_retries, delay)
                time.sleep(delay)
                continue
            logger.error("Gemini API error %d: %s", e.code, body[:500])
            return last_error
        except Exception as e:
            logger.error("Gemini API call failed: %s", e)
            return f"Error: {str(e)}"

    return last_error or "Gemini API error: max retries exceeded"


class GeminiAnalyzeController(Controller):
    """POST /api/gemini/analyze - analyze current frequency with Gemini AI"""

    def indexAction(self):
        """Handle POST with frequency data, optionally with captured audio"""
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
            mode = data.get('mode', 'unknown')
            bandwidth = data.get('bandwidth', '')
            squelch = data.get('squelch', '')
            has_audio = data.get('has_audio', False)
            custom_question = data.get('question', '')

            freq_mhz = freq_hz / 1e6

            # Collect audio if available
            audio_wav = None
            if has_audio:
                audio_wav = stop_and_get_audio()
                audio_info = f"\nHo allegato un campione audio di circa {CAPTURE_DURATION} secondi catturato su questa frequenza."
            else:
                audio_info = "\nNon ho un campione audio al momento."

            # Build the prompt
            squelch_str = f"{squelch} dB" if squelch else "non impostato"
            prompt = f"""Sei un esperto di radio e telecomunicazioni e un esperto linguista poliglotta. Analizza questa sintonizzazione radio in modo CONCISO e SINTETICO.

**Frequenza:** {freq_mhz:.6f} MHz ({freq_hz} Hz)
**Modo:** {mode.upper()} | **Banda:** {bandwidth} Hz | **Squelch:** {squelch_str}
**Posizione ricevitore:** Bolzano/Bozen, Alto Adige/Südtirol, Italia (zona bilingue italiano/tedesco)
**UTC:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}
{audio_info}

IMPORTANTE: Rispondi in italiano, in modo BREVE e RIASSUNTIVO (max 15-20 righe). Niente lunghi paragrafi.

Rispondi con questo formato:

**ETICHETTA_SUGGERITA:** [Suggerisci un nome breve e descrittivo per questa frequenza, da usare come bookmark. Esempio: "RAI Radio 1", "Guardia Costiera", "Banda 40m SSB", "VOLMET Milano", "PMR446 Ch1", "Aeronautica 124.5". Deve essere conciso (max 5-6 parole)]

**Identificazione:** Cosa trasmette su questa frequenza? (1-2 frasi)
**Banda/Allocazione:** Banda e allocazione Regione 1 (1 frase)
**Segnale:** Tipo di modulazione/segnale atteso (1 frase)
**Consigli:** Orari e note per l'ascolto (1-2 frasi)
"""
            if custom_question:
                prompt += f"\n**Domanda utente:** {custom_question}\n"

            if audio_wav:
                audio_dur = (len(audio_wav) - 44) / (SAMPLE_RATE * 2)
                prompt += f"""\n**Audio allegato** ({audio_dur:.1f}s dal ricevitore SDR).

ANALISI AUDIO RICHIESTA - Rispondi con queste sezioni aggiuntive:

**Lingua:** Identifica la lingua parlata nell'audio (può essere italiano, tedesco, dialetto sudtirolese/bavarese, inglese, o altro). Il ricevitore è a Bolzano, quindi è molto comune sentire tedesco e dialetto sudtirolese.
**Trascrizione:** Trascrivi il più fedelmente possibile quello che viene detto nell'audio. Se la qualità è scarsa, trascrivi quello che riesci a capire e indica le parti incerte con [?] o [...]
**Traduzione:** Se la lingua NON è italiano, fornisci la traduzione in italiano di quello che viene detto.
**Contenuto:** Descrivi brevemente cosa senti (voce, musica, dati, rumore, portante, comunicazione radio bidirezionale, ecc.) e il contesto della comunicazione.
"""

            # Call Gemini
            response_text = _call_gemini(prompt, audio_wav)

            # Extract the suggested label from Gemini's response
            import re as _re
            suggested_label = ""
            label_match = _re.search(r'\*\*ETICHETTA_SUGGERITA:\*\*\s*(.+)', response_text)
            if label_match:
                suggested_label = label_match.group(1).strip().strip('"').strip("'").strip('*').strip()

            self.send_response(
                json.dumps({
                    "success": True,
                    "frequency_mhz": freq_mhz,
                    "frequency_hz": freq_hz,
                    "mode": mode,
                    "analysis": response_text,
                    "suggested_label": suggested_label,
                    "had_audio": audio_wav is not None,
                    "audio_duration_s": (len(audio_wav) - 44) / (SAMPLE_RATE * 2) if audio_wav else 0
                }),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        except Exception as e:
            logger.error("Gemini analyze error: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )


class GeminiCaptureController(Controller):
    """POST /api/gemini/capture - start audio capture for Gemini"""

    def indexAction(self):
        try:
            start_audio_capture()
            self.send_response(
                json.dumps({"success": True, "capturing": True, "duration": CAPTURE_DURATION}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )


class GeminiBookmarkController(Controller):
    """POST /api/gemini/bookmark - create a bookmark from Gemini's suggestion"""

    def indexAction(self):
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
            name = data.get('name', '').strip()
            freq_hz = int(data.get('frequency', 0))
            modulation = data.get('modulation', '').strip().lower()
            description = data.get('description', '').strip()

            if not name or freq_hz <= 0 or not modulation:
                self.send_response(
                    json.dumps({"error": "name, frequency (>0) and modulation are required"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            from owrx.bookmarks import Bookmarks, Bookmark
            from owrx.modes import Modes

            # Validate modulation
            mode = Modes.findByModulation(modulation)
            if mode is None:
                # Try common aliases
                alias_map = {"usb": "usb", "lsb": "lsb", "am": "am", "nfm": "nfm",
                             "wfm": "wfm", "cw": "cw", "dmr": "dmr", "dstar": "dstar",
                             "ft8": "ft8", "ft4": "ft4", "wspr": "wspr", "sstv": "sstv"}
                modulation = alias_map.get(modulation, modulation)
                mode = Modes.findByModulation(modulation)

            if mode is None:
                self.send_response(
                    json.dumps({"error": f"Unknown modulation: {modulation}"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            bm_data = {
                "name": name,
                "frequency": freq_hz,
                "modulation": modulation,
                "description": description,
            }

            bookmarks = Bookmarks.getSharedInstance()
            b = Bookmark(bm_data)
            bookmarks.addBookmark(b)
            bookmarks.store()

            logger.info("Bookmark created from Gemini: '%s' @ %d Hz (%s)", name, freq_hz, modulation)

            self.send_response(
                json.dumps({"success": True, "bookmark": bm_data}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        except Exception as e:
            logger.error("Gemini bookmark error: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )


class GeminiRecordingController(Controller):
    """POST /api/gemini/analyze-recording - analyze a saved recording with Gemini AI"""

    RECORDINGS_DIR = "/var/lib/openwebrx/recordings"

    def indexAction(self):
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
            filename = data.get('filename', '').strip()
            if not filename or '..' in filename or '/' in filename:
                self.send_response(
                    json.dumps({"error": "Invalid filename"}),
                    content_type="application/json", code=400,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            filepath = os.path.join(self.RECORDINGS_DIR, filename)
            if not os.path.isfile(filepath):
                self.send_response(
                    json.dumps({"error": f"File not found: {filename}"}),
                    content_type="application/json", code=404,
                    headers={"Access-Control-Allow-Origin": "*"}
                )
                return

            # Convert MP3 to WAV using ffmpeg (Gemini needs WAV)
            import subprocess
            import tempfile
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                tmp_path = tmp.name
            try:
                result = subprocess.run(
                    ['ffmpeg', '-y', '-i', filepath, '-ar', '16000', '-ac', '1',
                     '-sample_fmt', 's16', '-t', '30', tmp_path],
                    capture_output=True, timeout=30
                )
                if result.returncode != 0:
                    self.send_response(
                        json.dumps({"error": "Failed to decode audio file"}),
                        content_type="application/json", code=500,
                        headers={"Access-Control-Allow-Origin": "*"}
                    )
                    return
                with open(tmp_path, 'rb') as wf:
                    audio_wav = wf.read()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            # Parse freq from filename (e.g. 6.0300MHz_20250101_120000.mp3)
            freq_str = ""
            import re as _re
            m = _re.match(r'([\d.]+)\s*MHz', filename)
            if m:
                freq_str = m.group(1) + " MHz"

            audio_dur = (len(audio_wav) - 44) / (16000 * 2) if len(audio_wav) > 44 else 0

            prompt = f"""Sei un esperto analista di segnali radio e telecomunicazioni. Ti invio un campione audio registrato da un ricevitore SDR (Software Defined Radio) con antenna Discone e LNA.

**File:** {filename}
**Frequenza:** {freq_str if freq_str else 'sconosciuta'}
**Durata audio:** {audio_dur:.1f} secondi
**Data/Ora UTC:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}
**Posizione ricevitore:** Bolzano, Alto Adige, Italia (46.5°N 11.3°E, Regione ITU 1)
**Antenna:** Discone artigianale + LNA (Nooelec NESDR SMArt v5, direct sampling HF)

Ascolta attentamente l'audio allegato e scrivi un **rapporto professionale in italiano** come farebbe un radioascoltatore esperto o un operatore di monitoraggio. Il rapporto deve essere un testo unico e fluido (NON una lista numerata, NON domande e risposte), in massimo 15-20 righe.

Il rapporto deve coprire: identificazione della stazione/servizio (basandoti su frequenza, orario, posizione del ricevitore e contenuto audio), tipo di trasmissione (broadcast, utility, numeri, militare, ecc.), valutazione della qualità di ricezione, e eventuali note tecniche rilevanti.

**IMPORTANTE — Trascrizione e traduzione:** Se nell'audio è presente parlato, musica con testo, annunci o qualsiasi contenuto vocale comprensibile:
- Indica la lingua rilevata.
- Trascrivi il più possibile di ciò che viene detto (anche se frammentario o parziale a causa del rumore).
- Fornisci la traduzione in italiano di tutto il parlato trascritto.
- Se si tratta di un notiziario, programma o discorso, riassumi l'argomento di cui parlano.
Se il contenuto è solo rumore, portante, dati digitali o segnale non vocale, descrivilo tecnicamente.

Usa un tono tecnico ma accessibile, come un bollettino di monitoraggio radio professionale.
"""
            custom_q = data.get('question', '')
            if custom_q:
                prompt += f"\n**Domanda aggiuntiva:** {custom_q}\n"

            response_text = _call_gemini(prompt, audio_wav)

            self.send_response(
                json.dumps({"text": response_text, "filename": filename}),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"}
            )

        except Exception as e:
            logger.error("Gemini recording analysis error: %s", e, exc_info=True)
            self.send_response(
                json.dumps({"error": str(e)}),
                content_type="application/json", code=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )
