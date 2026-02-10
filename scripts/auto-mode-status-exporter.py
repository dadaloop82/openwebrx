#!/usr/bin/env python3
"""
Auto Mode Status Exporter
Exports auto-mode status to JSON file for web access
Runs as a background daemon
"""

import sys
import os
import json
import time
import logging
from pathlib import Path

# Setup paths
sys.path.insert(0, '/opt/openwebrx-fork')

from owrx.auto_mode_init import get_auto_mode_status

# Configuration
OUTPUT_FILE = '/var/www/html/auto-mode-status.json'
UPDATE_INTERVAL = 5  # seconds

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def export_status():
    """Export auto-mode status to JSON file"""
    try:
        status = get_auto_mode_status()
        
        # Ensure output directory exists
        output_dir = Path(OUTPUT_FILE).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Write to temporary file first
        temp_file = OUTPUT_FILE + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(status, f, indent=2, default=str)
        
        # Atomic rename
        os.rename(temp_file, OUTPUT_FILE)
        
        logger.debug("Exported status: %d bytes", os.path.getsize(OUTPUT_FILE))
        
    except Exception as e:
        logger.error("Error exporting status: %s", e, exc_info=True)


def main():
    """Main loop"""
    logger.info("Auto-Mode Status Exporter started")
    logger.info("Output file: %s", OUTPUT_FILE)
    logger.info("Update interval: %d seconds", UPDATE_INTERVAL)
    
    try:
        while True:
            export_status()
            time.sleep(UPDATE_INTERVAL)
            
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
