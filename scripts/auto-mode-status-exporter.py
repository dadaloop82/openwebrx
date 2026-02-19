#!/usr/bin/env python3
"""
Auto Mode Status Exporter
Exports auto-mode status to JSON file for web access via HTTP API
Runs as a background daemon
"""

import json
import time
import logging
import requests
from pathlib import Path

# Configuration
OPENWEBRX_URL = 'http://localhost:8073/api/auto-mode/status'
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
        # Query the HTTP API
        response = requests.get(OPENWEBRX_URL, timeout=2)
        response.raise_for_status()
        status = response.json()
        
        # Ensure output directory exists
        output_dir = Path(OUTPUT_FILE).parent
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Write to temporary file first
        temp_file = OUTPUT_FILE + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(status, f, indent=2)
        
        # Atomic rename
        import os
        os.rename(temp_file, OUTPUT_FILE)
        
        logger.debug("Exported status from OpenWebRX API")
        return True
        
    except requests.RequestException as e:
        logger.warning(f"Failed to query OpenWebRX API: {e}")
        return False
    except Exception as e:
        logger.error(f"Error exporting status: {e}")
        return False


def main():
    logger.info("Auto-Mode Status Exporter started")
    logger.info(f"OpenWebRX API: {OPENWEBRX_URL}")
    logger.info(f"Output file: {OUTPUT_FILE}")
    logger.info(f"Update interval: {UPDATE_INTERVAL} seconds")
    
    while True:
        try:
            export_status()
            time.sleep(UPDATE_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Exporter stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(UPDATE_INTERVAL)


if __name__ == '__main__':
    main()
