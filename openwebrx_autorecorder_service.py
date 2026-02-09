#!/usr/bin/env python3
"""
Script di integrazione dell'auto-recorder con OpenWebRX
Da eseguire come servizio systemd
"""

import sys
import os

# Aggiungi il path di OpenWebRX
sys.path.insert(0, '/opt/openwebrx-fork')

from owrx.auto_recorder import AutoRecorder
import logging
import signal
import time

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/var/log/openwebrx-autorecorder.log')
    ]
)

logger = logging.getLogger(__name__)

# Variabili globali
recorder = None
running = True


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global running, recorder
    logger.info("Received shutdown signal")
    running = False
    if recorder:
        recorder.stop()
    sys.exit(0)


def main():
    """Main entry point"""
    global recorder, running
    
    # Registra handler per i segnali
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("Starting OpenWebRX Auto-Recorder service...")
    
    try:
        # Avvia l'auto-recorder
        recorder = AutoRecorder.get_instance()
        recorder.start()
        
        # Mantieni il servizio attivo
        while running:
            time.sleep(1)
            
    except Exception as e:
        logger.error("Fatal error: %s", e, exc_info=True)
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
