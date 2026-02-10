"""
Recording Scheduler for OpenWebRX
Allows scheduling automatic recordings for specific frequencies at specific times
Configuration via JSON file
"""

import os
import json
import time
import logging
import threading
import subprocess
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, List, Optional
from owrx.config.core import CoreConfig

logger = logging.getLogger(__name__)


class ScheduledRecording:
    """Represents a scheduled recording"""
    
    def __init__(self, config: Dict):
        self.id = config.get('id', 'unnamed')
        self.name = config.get('name', 'Unnamed Recording')
        self.frequency = config.get('frequency', 0)  # Hz
        self.mode = config.get('mode', 'USB')
        self.enabled = config.get('enabled', True)
        
        # Schedule configuration
        self.days_of_week = config.get('days_of_week', [0, 1, 2, 3, 4, 5, 6])  # 0=Monday
        self.start_time = self._parse_time(config.get('start_time', '00:00'))
        self.duration = config.get('duration_minutes', 60) * 60  # Convert to seconds
        
        # Recording parameters
        self.sample_rate = config.get('sample_rate', 48000)
        self.bitrate = config.get('bitrate', '128k')
        self.format = config.get('format', 'mp3')
        
        # Current state
        self.current_process = None
        self.current_filename = None
        self.recording_start_time = None
    
    def _parse_time(self, time_str: str) -> dt_time:
        """Parse time string HH:MM to time object"""
        try:
            h, m = map(int, time_str.split(':'))
            return dt_time(hour=h, minute=m)
        except:
            logger.error(f"Invalid time format: {time_str}, using 00:00")
            return dt_time(hour=0, minute=0)
    
    def should_record_now(self) -> bool:
        """Check if this recording should be active now"""
        if not self.enabled:
            return False
        
        now = datetime.now()
        
        # Check day of week
        if now.weekday() not in self.days_of_week:
            return False
        
        # Check time range
        current_time = now.time()
        start_datetime = datetime.combine(now.date(), self.start_time)
        end_datetime = start_datetime + timedelta(seconds=self.duration)
        
        # Handle recordings that span midnight
        if end_datetime.date() > now.date():
            # Recording spans midnight
            return current_time >= self.start_time or current_time < end_datetime.time()
        else:
            # Normal case
            return self.start_time <= current_time < end_datetime.time()
    
    def get_recording_time_remaining(self) -> int:
        """Get seconds remaining in current recording window, 0 if not in window"""
        if not self.should_record_now():
            return 0
        
        now = datetime.now()
        start_datetime = datetime.combine(now.date(), self.start_time)
        
        # If we're past the start time today, that's our start
        if now.time() >= self.start_time:
            end_datetime = start_datetime + timedelta(seconds=self.duration)
        else:
            # Must be a recording from yesterday that spans midnight
            start_datetime -= timedelta(days=1)
            end_datetime = start_datetime + timedelta(seconds=self.duration)
        
        remaining = (end_datetime - now).total_seconds()
        return max(0, int(remaining))
    
    def __str__(self):
        return (f"ScheduledRecording({self.name}, {self.frequency/1e6:.3f}MHz, "
                f"{self.mode}, {self.start_time}-{self.duration//60}min)")


class RecordingScheduler:
    """Manages scheduled recordings"""
    
    instance = None
    lock = threading.Lock()
    
    @staticmethod
    def get_instance():
        with RecordingScheduler.lock:
            if RecordingScheduler.instance is None:
                RecordingScheduler.instance = RecordingScheduler()
        return RecordingScheduler.instance
    
    def __init__(self):
        self.config_file = os.path.join(
            CoreConfig().get_data_directory(),
            'recording_schedule.json'
        )
        self.recording_dir = CoreConfig().get_temporary_directory()
        self.schedules: List[ScheduledRecording] = []
        self.monitoring_thread = None
        self.running = False
        self.active_recordings: Dict[str, ScheduledRecording] = {}
        
        os.makedirs(self.recording_dir, exist_ok=True)
        self._load_schedules()
        
        logger.info("RecordingScheduler initialized - config: %s", self.config_file)
    
    def _load_schedules(self):
        """Load recording schedules from JSON file"""
        if not os.path.exists(self.config_file):
            self._create_default_config()
            return
        
        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)
                self.schedules = [
                    ScheduledRecording(sched) 
                    for sched in config.get('schedules', [])
                ]
            logger.info("Loaded %d recording schedules", len(self.schedules))
        except Exception as e:
            logger.error("Failed to load schedules: %s", e)
            self.schedules = []
    
    def _create_default_config(self):
        """Create default configuration file with examples"""
        default_config = {
            "version": "1.0",
            "schedules": [
                {
                    "id": "example_hf_net",
                    "name": "80m Net Recording",
                    "frequency": 3800000,
                    "mode": "LSB",
                    "enabled": False,
                    "days_of_week": [0, 2, 4],
                    "start_time": "19:00",
                    "duration_minutes": 60,
                    "sample_rate": 48000,
                    "bitrate": "128k",
                    "format": "mp3"
                },
                {
                    "id": "example_broadcast",
                    "name": "Shortwave Broadcast",
                    "frequency": 7200000,
                    "mode": "AM",
                    "enabled": False,
                    "days_of_week": [0, 1, 2, 3, 4, 5, 6],
                    "start_time": "18:00",
                    "duration_minutes": 120,
                    "sample_rate": 48000,
                    "bitrate": "128k",
                    "format": "mp3"
                }
            ]
        }
        
        try:
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info("Created default schedule configuration at %s", self.config_file)
        except Exception as e:
            logger.error("Failed to create default config: %s", e)
    
    def reload_schedules(self):
        """Reload schedules from config file"""
        logger.info("Reloading schedules...")
        self._load_schedules()
    
    def start(self):
        """Start the scheduler"""
        if self.running:
            logger.warning("RecordingScheduler already running")
            return
        
        self.running = True
        self.monitoring_thread = threading.Thread(target=self._monitor_schedules, daemon=True)
        self.monitoring_thread.start()
        
        logger.info("═══════════════════════════════════════════════════")
        logger.info("📅 RECORDING SCHEDULER STARTED")
        logger.info("   Schedules loaded: %d", len(self.schedules))
        logger.info("   Config file: %s", self.config_file)
        logger.info("   Output dir: %s", self.recording_dir)
        logger.info("═══════════════════════════════════════════════════")
        
        # Log active schedules
        for sched in self.schedules:
            if sched.enabled:
                logger.info("   ✓ %s", sched)
    
    def stop(self):
        """Stop the scheduler"""
        self.running = False
        
        # Stop all active recordings
        for recording_id in list(self.active_recordings.keys()):
            self._stop_recording(recording_id)
        
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=5)
        
        logger.info("RecordingScheduler stopped")
    
    def _monitor_schedules(self):
        """Monitor and trigger scheduled recordings"""
        while self.running:
            try:
                current_time = datetime.now()
                
                for schedule in self.schedules:
                    if not schedule.enabled:
                        continue
                    
                    should_record = schedule.should_record_now()
                    is_recording = schedule.id in self.active_recordings
                    
                    if should_record and not is_recording:
                        # Start new recording
                        self._start_recording(schedule)
                    
                    elif not should_record and is_recording:
                        # Stop recording
                        self._stop_recording(schedule.id)
                    
                    elif is_recording:
                        # Check if recording time limit reached
                        remaining = schedule.get_recording_time_remaining()
                        if remaining <= 0:
                            self._stop_recording(schedule.id)
                
                time.sleep(10)  # Check every 10 seconds
                
            except Exception as e:
                logger.error("Error in schedule monitoring: %s", e)
                time.sleep(10)
    
    def _start_recording(self, schedule: ScheduledRecording):
        """Start a scheduled recording"""
        try:
            # Generate filename
            start_time = datetime.now()
            timestamp = start_time.strftime('%Y%m%d_%H%M%S')
            freq_mhz = schedule.frequency / 1_000_000
            filename = f"SCHED_{schedule.id}_{freq_mhz:.3f}MHz_{timestamp}.{schedule.format}"
            filepath = os.path.join(self.recording_dir, filename)
            
            # Build ffmpeg command
            # Note: This is a placeholder - needs integration with OpenWebRX audio pipeline
            cmd = [
                'ffmpeg',
                '-f', 'pulse',
                '-i', 'default',
                '-acodec', 'libmp3lame' if schedule.format == 'mp3' else 'copy',
                '-b:a', schedule.bitrate,
                '-ar', str(schedule.sample_rate),
                '-t', str(schedule.get_recording_time_remaining()),  # Max duration
                '-y',
                filepath
            ]
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            
            schedule.current_process = process
            schedule.current_filename = filename
            schedule.recording_start_time = start_time
            self.active_recordings[schedule.id] = schedule
            
            logger.info("📼 SCHEDULED RECORDING STARTED: %s", schedule.name)
            logger.info("   File: %s", filename)
            logger.info("   Frequency: %.3f MHz", freq_mhz)
            logger.info("   Mode: %s", schedule.mode)
            logger.info("   Duration: %d minutes", schedule.duration // 60)
            
            # Inject metadata if mutagen is available
            try:
                from mutagen.mp3 import MP3
                from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, COMM, TXXX
                
                def inject_metadata():
                    time.sleep(3)  # Wait for file to be created
                    try:
                        audio = MP3(filepath, ID3=ID3)
                        try:
                            audio.add_tags()
                        except:
                            pass
                        
                        audio.tags.add(TIT2(encoding=3, text=f"[SCHEDULED] {schedule.name} - {freq_mhz:.3f} MHz"))
                        audio.tags.add(TPE1(encoding=3, text="OpenWebRX Scheduler"))
                        audio.tags.add(TALB(encoding=3, text=f"Scheduled Recordings - {schedule.mode}"))
                        audio.tags.add(TDRC(encoding=3, text=start_time.strftime('%Y')))
                        
                        comment = (
                            f"Scheduled Recording\n"
                            f"Name: {schedule.name}\n"
                            f"Frequency: {freq_mhz:.6f} MHz\n"
                            f"Mode: {schedule.mode}\n"
                            f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                            f"Duration: {schedule.duration // 60} minutes"
                        )
                        audio.tags.add(COMM(encoding=3, lang='eng', desc='Schedule Info', text=comment))
                        audio.tags.add(TXXX(encoding=3, desc='Frequency_Hz', text=str(schedule.frequency)))
                        audio.tags.add(TXXX(encoding=3, desc='Mode', text=schedule.mode))
                        audio.tags.add(TXXX(encoding=3, desc='Schedule_ID', text=schedule.id))
                        
                        audio.save()
                        logger.info("🏷️  Metadata injected for scheduled recording")
                    except Exception as e:
                        logger.error("Failed to inject metadata: %s", e)
                
                threading.Thread(target=inject_metadata, daemon=True).start()
            except ImportError:
                pass
            
        except Exception as e:
            logger.error("Failed to start scheduled recording %s: %s", schedule.name, e)
    
    def _stop_recording(self, recording_id: str):
        """Stop a scheduled recording"""
        if recording_id not in self.active_recordings:
            return
        
        schedule = self.active_recordings[recording_id]
        
        try:
            if schedule.current_process:
                schedule.current_process.terminate()
                schedule.current_process.wait(timeout=5)
                
                duration = (datetime.now() - schedule.recording_start_time).total_seconds()
                logger.info("⏹️  SCHEDULED RECORDING STOPPED: %s", schedule.name)
                logger.info("   File: %s", schedule.current_filename)
                logger.info("   Duration: %.1f minutes", duration / 60)
        except Exception as e:
            logger.error("Error stopping recording %s: %s", schedule.name, e)
            try:
                schedule.current_process.kill()
            except:
                pass
        
        schedule.current_process = None
        schedule.current_filename = None
        schedule.recording_start_time = None
        del self.active_recordings[recording_id]
    
    def get_status(self) -> Dict:
        """Get current scheduler status"""
        return {
            'running': self.running,
            'total_schedules': len(self.schedules),
            'enabled_schedules': len([s for s in self.schedules if s.enabled]),
            'active_recordings': len(self.active_recordings),
            'schedules': [
                {
                    'id': s.id,
                    'name': s.name,
                    'frequency': s.frequency,
                    'mode': s.mode,
                    'enabled': s.enabled,
                    'recording': s.id in self.active_recordings,
                    'should_record_now': s.should_record_now(),
                    'time_remaining': s.get_recording_time_remaining() if s.should_record_now() else 0
                }
                for s in self.schedules
            ]
        }


def init_recording_scheduler():
    """Initialize and start the recording scheduler"""
    try:
        scheduler = RecordingScheduler.get_instance()
        scheduler.start()
    except Exception as e:
        logger.error("Failed to initialize RecordingScheduler: %s", e)


if __name__ == "__main__":
    # Test the scheduler
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    
    scheduler = RecordingScheduler.get_instance()
    scheduler.start()
    
    print("\n📅 Recording Scheduler Test")
    print("=" * 50)
    print(f"Config file: {scheduler.config_file}")
    print(f"Loaded schedules: {len(scheduler.schedules)}")
    print("\nPress Ctrl+C to stop...\n")
    
    try:
        while True:
            status = scheduler.get_status()
            print(f"\rActive recordings: {status['active_recordings']} | "
                  f"Enabled schedules: {status['enabled_schedules']}", end='', flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n\nStopping scheduler...")
        scheduler.stop()
        print("✅ Stopped")
