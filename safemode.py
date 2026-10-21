"""Runs instead of code.py when CircuitPython enters safe mode (after a crash in
CircuitPython itself, a brownout from a nearly empty power bank, etc.).

In safe mode the board would otherwise wait for a USB connection forever, and the
saber is used with only its button. So: note the reason in the error log that
saber.py prints at startup, then reset and start normally.
"""
import microcontroller
import supervisor

NVM_LOG_LENGTH = 1  # must match saber.py
NVM_LOG_START = 3

try:
    reason = supervisor.runtime.safe_mode_reason
except Exception:
    reason = "unknown"
print("Safe mode:", reason)

try:
    nvm = microcontroller.nvm
    used = nvm[NVM_LOG_LENGTH] | (nvm[NVM_LOG_LENGTH + 1] << 8)
    if used > len(nvm) - NVM_LOG_START:
        used = 0  # erased flash
    entry = "--- safe mode: {}\n".format(reason).encode()
    new_length = used + len(entry)
    if NVM_LOG_START + new_length <= len(nvm):
        nvm[NVM_LOG_LENGTH:NVM_LOG_START + new_length] = (
            bytes((new_length & 0xFF, new_length >> 8))
            + bytes(nvm[NVM_LOG_START:NVM_LOG_START + used])
            + entry
        )
except Exception:
    pass

try:
    import time
    time.sleep(2)  # e.g. after a brownout, give the power bank a moment
except Exception:
    pass
microcontroller.reset()
