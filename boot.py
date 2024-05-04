# SPDX-FileCopyrightText: 2017 Limor Fried for Adafruit Industries
#
# SPDX-License-Identifier: MIT

import board
import digitalio
import storage

# To update software, EXTERNAL_BUTTON must be pressed when plugging
# USB cable, so storage is mounted rw for PC
switch = digitalio.DigitalInOut(board.EXTERNAL_BUTTON)
switch.direction = digitalio.Direction.INPUT
switch.pull = digitalio.Pull.UP

# If the D0 is connected to ground with a wire
# CircuitPython can write to the drive
storage.remount("/", readonly=not switch.value)