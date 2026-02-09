"""
Custom Writer for monitoring squelch state changes
"""

from csdr.module import Writer
import logging

logger = logging.getLogger(__name__)


class SquelchMonitorWriter(Writer):
    """Writer that monitors squelch state and triggers recording"""
    
    def __init__(self, frequency_callback=None, squelch_callback=None):
        super().__init__()
        self.frequency_callback = frequency_callback
        self.squelch_callback = squelch_callback
        self.is_open = False
        self.frequency_hz = None
        logger.debug("SquelchMonitorWriter initialized")
    
    def setFrequency(self, frequency_hz: int):
        """Update current frequency"""
        self.frequency_hz = frequency_hz
        if self.frequency_callback:
            try:
                self.frequency_callback(frequency_hz)
            except Exception as e:
                logger.error("Error in frequency callback: %s", e)
    
    def write(self, data: bytes):
        """
        Called by squelch module with power readings
        Data format: struct with power values
        When squelch opens/closes, we detect state change
        """
        if not self.squelch_callback:
            return
        
        try:
            # Parse squelch data - typically includes power level
            # When power exceeds threshold, squelch is "open"
            # This is called with power measurements
            
            # For now, we'll detect state changes through power levels
            # The actual squelch module handles open/close internally
            # We hook into the chain's audio output instead
            pass
            
        except Exception as e:
            logger.error("Error in squelch writer: %s", e)
    
    def onSquelchOpen(self):
        """Called when squelch opens"""
        if not self.is_open:
            self.is_open = True
            if self.squelch_callback:
                try:
                    self.squelch_callback('open', self.frequency_hz)
                    logger.debug("Squelch OPEN at %.3f MHz", (self.frequency_hz or 0) / 1e6)
                except Exception as e:
                    logger.error("Error in squelch open callback: %s", e)
    
    def onSquelchClose(self):
        """Called when squelch closes"""
        if self.is_open:
            self.is_open = False
            if self.squelch_callback:
                try:
                    self.squelch_callback('close', self.frequency_hz)
                    logger.debug("Squelch CLOSED at %.3f MHz", (self.frequency_hz or 0) / 1e6)
                except Exception as e:
                    logger.error("Error in squelch close callback: %s", e)
