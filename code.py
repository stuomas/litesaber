"""Entry point. Kept small on purpose: the board wakes up every KEEPALIVE_INTERVAL
seconds just to keep the power bank on, and CircuitPython compiles the whole of
code.py on every wakeup. The lightsaber itself is in saber.py, which is only
imported (and compiled) when the button is pressed or power is connected.

Rule for this file: it must never just end. If code.py finishes, CircuitPython turns
off the watchdog and waits for a USB connection forever, so every path here ends in
deep sleep or a reset.
"""
import alarm
import board
import microcontroller
from microcontroller import watchdog as wdog
import time
from watchdog import WatchDogMode

# Anker PowerCore 5000 turns off after ~2 minutes if power consumption is under
# some limit. To prevent that, wake up periodically from deep sleep and draw some current:
# the LED strips are powered for KEEPALIVE_LOAD_SEC (two strips of 72 LEDs draw ~100 mA
# even when dark) and one pixel flashes. A 50 ms flash alone was too short for the power bank.
KEEPALIVE_INTERVAL = 90
KEEPALIVE_LOAD_SEC = 1.5
WATCHDOG_TIMEOUT = 4.0


def sleep_until_alarms(keepalive=True):
    """Deep sleep until the button is pressed or, with keepalive, the keepalive timer fires.
    Without keepalive the power bank switches itself off after a while. Does not return."""
    pin_alarm = alarm.pin.PinAlarm(pin=board.EXTERNAL_BUTTON, value=False, edge=True, pull=True)
    if keepalive:
        time_alarm = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + KEEPALIVE_INTERVAL)
        alarm.exit_and_deep_sleep_until_alarms(pin_alarm, time_alarm)
    else:
        alarm.exit_and_deep_sleep_until_alarms(pin_alarm)


def enable_watchdog():
    wdog.timeout = WATCHDOG_TIMEOUT
    wdog.mode = WatchDogMode.RESET


def keepalive_and_sleep():
    """Power the LED strips for a moment and flash one pixel, so the power bank sees a
    load and stays on, then go back to sleep."""
    enable_watchdog()
    print("Keepalive")
    try:
        from digitalio import DigitalInOut, Direction
        import neopixel

        external_power = DigitalInOut(board.EXTERNAL_POWER)
        external_power.direction = Direction.OUTPUT
        external_power.value = True
        pixels = neopixel.NeoPixel(board.EXTERNAL_NEOPIXELS, 1, auto_write=False)
        pixels.brightness = 0.95
        pixels[0] = (128, 128, 128)
        pixels.show()
        time.sleep(0.05)
        pixels[0] = (0, 0, 0)
        pixels.show()
        time.sleep(KEEPALIVE_LOAD_SEC)  # keep the strips powered: this is the load that counts
    except Exception as e:
        print("Keepalive flash failed:", repr(e))  # still sleep and keep waiting for the button
    sleep_until_alarms()


def start_saber():
    # Imported before the watchdog starts: compiling saber.py takes a moment
    try:
        import saber
    except Exception as e:
        # saber.py missing or broken (e.g. only code.py was copied): nothing to run.
        # Keep the keepalive going and retry on the next button press.
        print("Cannot load saber.py:", repr(e))
        time.sleep(2)
        sleep_until_alarms()

    enable_watchdog()
    try:
        saber.run(sleep_until_alarms)
    except Exception as e:
        saber.log_error("Crashed", e)
        saber.restart_after_error()


try:
    if isinstance(alarm.wake_alarm, alarm.time.TimeAlarm):
        keepalive_and_sleep()
    else:
        start_saber()
except Exception as e:
    # Last resort, e.g. deep sleep itself failed. Deep sleep requests are not Exceptions,
    # so normal sleeping passes through here untouched.
    print("Unexpected error:", repr(e))
    time.sleep(1)
    microcontroller.reset()
