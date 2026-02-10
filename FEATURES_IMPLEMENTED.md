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
