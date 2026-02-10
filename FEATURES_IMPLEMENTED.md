# 🎯 New Features Implemented

This document describes the new features added to this OpenWebRX fork.

## ✅ Feature #1: Recording Scheduler 📅

**Status**: Implemented and Tested

Schedule automatic audio recordings for specific frequencies at specific times.

### Features:
- JSON-based configuration (`recording_schedule.json`)
- Recurring schedules (daily, specific days of week)
- Automatic start/stop based on time windows
- Handles midnight-spanning recordings
- Integration with metadata injection
- Status reporting API
- Example configurations included

### Configuration Example:
```json
{
  "version": "1.0",
  "schedules": [
    {
      "id": "evening_net",
      "name": "80m Net Recording",
      "frequency": 3800000,
      "mode": "LSB",
      "enabled": true,
      "days_of_week": [0, 2, 4],
      "start_time": "19:00",
      "duration_minutes": 60,
      "sample_rate": 48000,
      "bitrate": "128k",
      "format": "mp3"
    }
  ]
}
```

### Files:
- `owrx/recording_scheduler.py` - Main scheduler module

---

## ✅ Feature #2: ID3 Metadata Injection 🏷️

**Status**: Implemented and Tested

Automatically inject metadata tags into all MP3 recordings.

### Features:
- Standard ID3 tags (Title, Artist, Album, Year)
- Detailed comments with recording info
- Custom tags (Frequency_Hz, Mode, Timestamp_UTC)
- Receiver information (name, location, operator)
- Supports both mutagen and eyed3 libraries
- Auto-injection 2 seconds after recording starts

### Tags Injected:
- **Title**: `7.200 MHz - 2026-02-10 07:38:31 UTC`
- **Artist**: Receiver name
- **Album**: Location + Mode
- **Comment**: Full recording details
- **Custom**: Exact frequency in Hz, mode, ISO timestamp

### Files:
- `owrx/auto_recorder.py` - Updated with metadata support

### Dependencies:
```bash
sudo apt install -y python3-mutagen
```

---

## ✅ Feature #3: Digital Voice Logger 📝

**Status**: Implemented and Tested

Comprehensive logging system for all digital voice transmissions.

### Features:
- Logs DMR, YSF, NXDN, D-Star, M17 transmissions
- Captures: talkgroup IDs, radio IDs, callsigns
- Dual format: CSV (for analysis) + JSON (for API)
- Daily log rotation
- Search functionality across multiple days
- Statistics tracking (unique sources, talkgroups)
- Auto-cleanup (30 days retention)
- Buffered writes for performance

### Data Captured:
- Timestamp
- Mode (DMR/YSF/NXDN/etc)
- Frequency
- Source callsign/ID
- Destination/Talkgroup
- Slot/Timeslot
- Color code
- RSSI, BER
- Duration

### Log Files:
- `digital_voice_logs/digital_voice_YYYYMMDD.csv`
- `digital_voice_logs/digital_voice_YYYYMMDD.json`

### API:
```python
from owrx.digital_voice_logger import DigitalVoiceLogger

logger = DigitalVoiceLogger.get_instance()

# Log transmission
logger.log_transmission('DMR', {
    'frequency': 438450000,
    'source': 'IU2VTX',
    'source_id': 2227001,
    'talkgroup_id': 2227,
    'slot': 2
})

# Get statistics
stats = logger.get_statistics('DMR')

# Search logs
results = logger.search_logs({'Mode': 'DMR'}, days_back=7)
```

### Files:
- `owrx/digital_voice_logger.py` - Main logger module

---

## 🚀 Future Features (Planned)

### Feature #4: Web UI for Recorder 🖥️
- Browser-based control panel
- Start/stop recordings manually
- View active recordings
- Schedule management interface
- Real-time status display

### Feature #5: Waterfall Screenshot Recording 📸
- Auto-capture waterfall during recordings
- Synchronized with audio files
- Configurable capture rate
- Useful for post-analysis

### Feature #6: Advanced Codec Support 🗜️
- Opus codec (better compression)
- AAC codec (Apple compatibility)
- FLAC lossless option
- Configurable per schedule

---

## 📚 Integration Guide

### Auto-start all features:

Edit `/opt/openwebrx-fork/owrx/__main__.py` or startup script:

```python
from owrx.auto_recorder import init_auto_recorder
from owrx.recording_scheduler import init_recording_scheduler  
from owrx.digital_voice_logger import init_digital_voice_logger

# Initialize all features
init_auto_recorder()
init_recording_scheduler()
init_digital_voice_logger()
```

---

## 📊 Monitoring

Check logs:
```bash
sudo journalctl -u openwebrx -f
```

View recordings:
```bash
ls -lh /var/lib/openwebrx/recordings/
```

View digital voice logs:
```bash
ls -lh /var/lib/openwebrx/digital_voice_logs/
```

---

## 🐛 Troubleshooting

### Metadata not showing
- Check if mutagen is installed: `python3 -c "import mutagen"`
- Install: `sudo apt install -y python3-mutagen`

### Scheduler not starting
- Check config file exists: `/var/lib/openwebrx/recording_schedule.json`
- Verify JSON syntax with: `python3 -m json.tool /var/lib/openwebrx/recording_schedule.json`

### Logger not writing
- Check directory permissions: `/var/lib/openwebrx/digital_voice_logs/`
- Verify disk space: `df -h`

---

## 📝 Credits

Features developed for OpenWebRX fork by analyzing community requests from:
- https://github.com/jketterl/openwebrx/issues
- https://github.com/jketterl/openwebrx/pulls

Inspired by most-requested features from the OpenWebRX community.

---

## 📄 License

Same as OpenWebRX - GNU Affero General Public License v3.0

---

## 🤖 AUTO MODE - Sistema Autonomo

**Data Implementazione**: 10 Febbraio 2026  
**Commit**: aa09ff18, 4a476e07

Sistema completo di registrazione automatica e decodifica che si attiva quando **nessun client remoto è connesso**.

### 🎯 Caratteristiche

- ✅ **Attivazione automatica** quando nessun client remoto connesso
- ✅ **Distingue client locali** (stesso IP/LAN) da remoti  
- ✅ **Scan ciclico** su frequenze configurabili
- ✅ **Registrazione audio** automatica durante scan
- ✅ **Decodifica digitale** (DMR/YSF/APRS/FT8/ADS-B/POCSAG/etc)
- ✅ **Salvataggio completo** audio + decodifiche in Files tab
- ✅ **Stato web** disponibile su http://IP:8080/auto-mode-status.json
- ✅ **Ripristino immediato** controllo utente su connessione remota

### 📦 Componenti

1. **ClientMonitor** - Monitora connessioni WebSocket (local vs remote)
2. **AutoTuner** - Controlla frequenza/modo/squelch del ricevitore
3. **DecoderManager** - Gestisce tutti i decoder digitali  
4. **AutoModeOrchestrator** - Coordinatore generale (state machine)
5. **AutoModeInit** - Inizializzazione e API di sistema
6. **StatusExporter** - Servizio background per export JSON

### ⚙️ Configurazione

File: `/var/lib/openwebrx/auto_mode_config.json`

```json
{
  "orchestrator": {
    "enabled": true,
    "frequencies": [
      {
        "frequency": 145800000,
        "mode": "NFM",
        "squelch": 0.15,
        "bandwidth": 12500,
        "dwell_time": 120,
        "label": "APRS 2m 145.800 MHz"
      }
    ],
    "cycle_mode": "sequential",
    "enable_recording": true,
    "enable_decoders": true
  }
}
```

### 📊 Output

- **Audio**: `/var/lib/openwebrx/recordings/recording_*.mp3`
- **Decodifiche**: `/var/lib/openwebrx/auto_mode_decodings/SESSION_ID/`
  - `session.json` - Metadati sessione
  - `decodings.json` - Tutte le decodifiche (JSON)
  - `decodings.csv` - Tutte le decodifiche (CSV)
  - `statistics.json` - Statistiche finali

### 🌐 API Web

**Endpoint**: `http://IP:8080/auto-mode-status.json`

**Formato**:
```json
{
  "initialized": true,
  "client_monitor": {
    "has_remote_clients": false,
    "auto_mode_allowed": true,
    "clients": {"total": 0, "local": 0, "remote": 0}
  },
  "orchestrator": {
    "state": "auto",
    "current_frequency": {
      "frequency": 145800000,
      "mode": "NFM",
      "label": "APRS 2m"
    },
    "total_frequencies": 7
  },
  "decoder_manager": {
    "is_recording": true,
    "total_decodings": 42
  }
}
```

**Aggiornamento**: Ogni 5 secondi via `auto-mode-exporter.service`

### 🔧 Servizi Systemd

1. **openwebrx.service** - Server principale (inizializza auto-mode)
2. **auto-mode-exporter.service** - Export JSON status per web

### 📖 Documentazione Completa

Vedi: [AUTO_MODE_README.md](AUTO_MODE_README.md)

---

## 📈 Statistiche Totali

- **Features Implementate**: 4
- **Nuovi Moduli Python**: 8
- **Righe Codice Aggiunte**: ~2800
- **File Modificati**: 3  
- **Servizi Systemd**: 2
- **Endpoint API**: 1
- **Formati Output**: JSON, CSV, MP3

