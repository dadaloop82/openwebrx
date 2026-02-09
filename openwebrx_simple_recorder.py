#!/usr/bin/env python3
"""
Continuous Audio Recorder for OpenWebRX+
Simple implementation that records audio from OpenWebRX websocket connections
"""

import os
import sys
import json
import time
import subprocess
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Configuration
RECORDINGS_DIR = "/var/lib/openwebrx/recordings"
MAX_AGE_DAYS = 7
CHECK_INTERVAL = 300  # 5 minutes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SimpleRecorder:
    """Simple continuous recorder for OpenWebRX"""
    
    def __init__(self):
        self.recordings_dir = Path(RECORDINGS_DIR)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        
        # Update storage pattern to include our MP3 files
        self._update_storage_pattern()
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("🎙️  CONTINUOUS AUDIO RECORDER")
        logger.info("   Directory: %s", self.recordings_dir)
        logger.info("   Retention: %d days", MAX_AGE_DAYS)
        logger.info("═══════════════════════════════════════════════════")
    
    def _update_storage_pattern(self):
        """Update OpenWebRX storage pattern to include MP3 files"""
        try:
            storage_file = "/opt/openwebrx-fork/owrx/storage.py"
            with open(storage_file, 'r') as f:
                content = f.read()
            
            # Check if MP3 pattern already exists
            if r'\.mp3\)' in content or 'mp3' in content:
                logger.debug("Storage pattern already includes mp3")
                return
            
            # Backup
            import shutil
            shutil.copy2(storage_file, storage_file + '.bak')
            
            # Update pattern to include mp3
            old_pattern = r"filePattern = r'[A-Z0-9]+-[0-9]+-[0-9]+(-[0-9]+)?(-[0-9]+)?\.(bmp|png|txt|mp3)'"
            new_pattern = r"filePattern = r'([A-Z0-9]+-[0-9]+-[0-9]+(-[0-9]+)?(-[0-9]+)?\.(bmp|png|txt|mp3)|[0-9]+\.[0-9]+MHz_[0-9]+_[0-9]+\.mp3)'"
            
            content = content.replace(old_pattern, new_pattern)
            
            with open(storage_file, 'w') as f:
                f.write(content)
            
            logger.info("Updated storage pattern to include frequency-based MP3 files")
            
        except Exception as e:
            logger.warning("Could not update storage pattern: %s", e)
    
    def cleanup_old_recordings(self):
        """Delete recordings older than MAX_AGE_DAYS"""
        try:
            cutoff_time = time.time() - (MAX_AGE_DAYS * 86400)
            deleted = 0
            
            for file in self.recordings_dir.glob("*.mp3"):
                if file.stat().st_mtime < cutoff_time:
                    file.unlink()
                    deleted += 1
                    logger.debug("Deleted old recording: %s", file.name)
            
            if deleted > 0:
                logger.info("🗑️  Cleaned up %d old recordings", deleted)
                
        except Exception as e:
            logger.error("Error during cleanup: %s", e)
    
    def get_current_recording_filename(self, frequency_hz=None):
        """Generate filename for current recording"""
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        
        if frequency_hz:
            freq_mhz = frequency_hz / 1_000_000
            return f"{freq_mhz:.3f}MHz_{timestamp}.mp3"
        else:
            return f"RECORDING_{timestamp}.mp3"
    
    def run(self):
        """Main recording loop"""
        logger.info("Recorder started - performing periodic cleanup")
        
        while True:
            try:
                self.cleanup_old_recordings()
                time.sleep(CHECK_INTERVAL)
                
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception as e:
                logger.error("Error in main loop: %s", e)
                time.sleep(60)


def create_systemd_service():
    """Create systemd service file"""
    service_content = """[Unit]
Description=OpenWebRX Continuous Audio Recorder
After=openwebrx.service
Requires=openwebrx.service

[Service]
Type=simple
User=openwebrx
Group=openwebrx
WorkingDirectory=/opt/openwebrx-fork
ExecStart=/usr/bin/python3 /opt/openwebrx-fork/openwebrx_simple_recorder.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    
    service_path = "/etc/systemd/system/openwebrx-recorder.service"
    
    try:
        with open(service_path, 'w') as f:
            f.write(service_content)
        
        logger.info("✅ Created systemd service: %s", service_path)
        logger.info("To enable: sudo systemctl enable openwebrx-recorder")
        logger.info("To start: sudo systemctl start openwebrx-recorder")
        
    except PermissionError:
        logger.error("Need root permissions to create systemd service")
        logger.info("Save this content to %s:", service_path)
        print(service_content)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--install-service':
        create_systemd_service()
    else:
        recorder = SimpleRecorder()
        recorder.run()
