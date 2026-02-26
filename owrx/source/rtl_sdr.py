from owrx.source.connector import ConnectorSource, ConnectorDeviceDescription
from owrx.command import Flag, Option
from typing import List
from owrx.form.input import Input, TextInput
from owrx.form.input.device import BiasTeeInput, DirectSamplingInput
from owrx.form.input.validator import Range
import os
import fcntl
import glob
import time

import logging

logger = logging.getLogger(__name__)

# ioctl number for USB device reset
USBDEVFS_RESET = 21780


class RtlSdrSource(ConnectorSource):
    def preStart(self):
        """Reset the RTL-SDR USB device before starting the connector.

        Some RTL-SDR dongles (especially R820T2-based ones) can get into a state
        where the PLL fails to lock and no IQ data is produced. A USB device reset
        before each start reliably fixes this.
        """
        try:
            self._resetRtlSdrUsb()
        except Exception:
            logger.warning("USB reset failed, continuing anyway", exc_info=True)

    def _resetRtlSdrUsb(self):
        """Find and reset RTL2832U USB devices (vendor 0x0bda, product 0x2838)."""
        target_serial = self.sdrProps["device"] if "device" in self.sdrProps else None
        reset_done = False

        for devpath in glob.glob("/sys/bus/usb/devices/*/"):
            try:
                vid_path = os.path.join(devpath, "idVendor")
                pid_path = os.path.join(devpath, "idProduct")
                if not os.path.exists(vid_path) or not os.path.exists(pid_path):
                    continue
                vid = open(vid_path).read().strip()
                pid = open(pid_path).read().strip()
                if vid != "0bda" or pid != "2838":
                    continue

                # If a specific serial is configured, match it
                if target_serial:
                    serial_path = os.path.join(devpath, "serial")
                    if os.path.exists(serial_path):
                        serial = open(serial_path).read().strip()
                        if serial != target_serial:
                            continue

                # Get the /dev/bus/usb path
                busnum = open(os.path.join(devpath, "busnum")).read().strip()
                devnum = open(os.path.join(devpath, "devnum")).read().strip()
                usb_dev = f"/dev/bus/usb/{int(busnum):03d}/{int(devnum):03d}"

                if os.path.exists(usb_dev):
                    logger.info("Resetting RTL-SDR USB device: %s", usb_dev)
                    fd = os.open(usb_dev, os.O_WRONLY)
                    fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                    os.close(fd)
                    reset_done = True
                    time.sleep(1)  # Give the device time to re-enumerate
                    logger.info("RTL-SDR USB reset completed successfully")
                    break
            except Exception:
                continue

        if not reset_done:
            logger.debug("No RTL-SDR USB device found to reset")

    def getCommandMapper(self):
        return (
            super()
            .getCommandMapper()
            .setBase("rtl_connector")
            .setMappings({"bias_tee": Flag("-b"), "direct_sampling": Option("-e")})
        )


class RtlSdrDeviceDescription(ConnectorDeviceDescription):
    def getName(self):
        return "RTL-SDR device"

    def getInputs(self) -> List[Input]:
        return super().getInputs() + [
            TextInput(
                "device",
                "Device identifier",
                infotext="Device serial number or index",
            ),
            BiasTeeInput(),
            DirectSamplingInput(),
        ]

    def getDeviceOptionalKeys(self):
        return super().getDeviceOptionalKeys() + ["device", "bias_tee", "direct_sampling"]

    def getProfileOptionalKeys(self):
        return super().getProfileOptionalKeys() + ["bias_tee", "direct_sampling"]

    def getSampleRateRanges(self) -> List[Range]:
        return [Range(250000, 3200000)]
