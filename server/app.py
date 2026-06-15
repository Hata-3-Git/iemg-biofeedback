"""
iEMG Monitor — local streaming server
=====================================
Bridges a BITalino device (Bluetooth Classic / SPP) to the browser UI over a
WebSocket. All signal processing matches the original Tkinter application:
the raw ADC value is converted to mV, RMS is computed over each 30-sample
window, and IEMG is the running sum of RMS values over a 1-second window.

The browser cannot talk to BITalino directly (Web Bluetooth is BLE-only, and
BITalino uses Bluetooth Classic), so this server does the hardware part in
Python — exactly where the `bitalino` library already works — and streams the
result to the front-end.

Run with real hardware:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000
    # then open http://localhost:8000 and choose "BITalino (local server)"

Run without hardware (synthetic signal, for testing the pipeline / UI):
    SIM=1 uvicorn app:app --port 8000
"""

import os
import asyncio
import math
import time
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

# --------------------------------------------------------------------------- #
# Constants — identical to the original BITalino application
# --------------------------------------------------------------------------- #
SAMPLING_RATE      = 1000                                  # Hz
SAMPLES_TO_READ    = 30                                    # samples per window (30 ms)
BITS               = 10                                    # ADC resolution
VCC                = 3.3                                   # V
GAIN               = 1009                                  # EMG sensor gain
CHANNEL_TO_MEASURE = [0]                                   # BITalino analog channel A1
ADC_COLUMN         = 5                                     # column of A1 in a BITalino frame

RMS_INTERVAL       = SAMPLES_TO_READ / SAMPLING_RATE       # 0.03 s
IEMG_WINDOW_S      = 1.0
IEMG_WINDOW_POINTS = int(IEMG_WINDOW_S / RMS_INTERVAL)     # 33

SIM_DEFAULT = os.getenv("SIM", "0") == "1"


def to_mv(raw: np.ndarray) -> np.ndarray:
    """ADC counts -> millivolts (matches the original conversion)."""
    return (((raw / (2 ** BITS)) - 0.5) * VCC / GAIN) * 1000.0


# --------------------------------------------------------------------------- #
# Signal sources
# --------------------------------------------------------------------------- #
class Source:
    """Common interface: read_window() returns SAMPLES_TO_READ ADC counts."""
    def read_window(self) -> np.ndarray:
        raise NotImplementedError

    def close(self):
        pass


class SimSource(Source):
    """Synthetic sEMG so the full pipeline runs without a device."""
    def __init__(self):
        self._t = 0.0

    def read_window(self) -> np.ndarray:
        time.sleep(RMS_INTERVAL)  # emulate the 30 ms acquisition cadence
        self._t += RMS_INTERVAL
        # slow envelope + occasional bursts -> lifelike activation
        env = 0.5 + 0.5 * math.sin(self._t * 1.3) * math.sin(self._t * 0.5 + 1.0)
        if np.random.rand() < 0.03:
            env = min(1.0, env + 0.5)
        sigma = 1.4 + max(0.0, env) * 110.0
        raw = 512 + np.random.randn(SAMPLES_TO_READ) * sigma
        return np.clip(raw, 0, (2 ** BITS) - 1)


class BITalinoSource(Source):
    """Real device via the `bitalino` library (MAC address or serial port)."""
    def __init__(self, mac_address: str):
        from bitalino import BITalino  # imported lazily so SIM mode needs no pybluez
        self.device = BITalino(mac_address)
        self.device.start(SAMPLING_RATE, CHANNEL_TO_MEASURE)

    def read_window(self) -> np.ndarray:
        data = self.device.read(SAMPLES_TO_READ)
        return data[:, ADC_COLUMN]

    def close(self):
        try:
            self.device.stop()
            self.device.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Per-session DSP state (mirrors the Tkinter app's update loop)
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self):
        self.rms_buffer: list[float] = []
        self.count = 0

    def process(self, raw_window: np.ndarray) -> dict:
        emg = to_mv(raw_window)                                  # mV
        rms = float(np.sqrt(np.mean(emg ** 2)))
        self.rms_buffer.append(rms)
        if len(self.rms_buffer) > IEMG_WINDOW_POINTS:
            self.rms_buffer.pop(0)
        iemg = float(np.sum(self.rms_buffer) * RMS_INTERVAL)
        self.count += 1
        return {
            "t": round(self.count * RMS_INTERVAL, 4),
            "emg": [round(float(v), 5) for v in emg],
            "rms": round(rms, 6),
            "iemg": round(iemg, 6),
        }


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="iEMG Monitor")


@app.get("/health")
def health():
    return {"ok": True, "sim": SIM_DEFAULT, "fs": SAMPLING_RATE, "gain": GAIN}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session()
    source: Source | None = None
    use_sim = SIM_DEFAULT
    mac = ""
    running = False

    async def recv_loop():
        """Handle control messages without blocking the stream."""
        nonlocal use_sim, mac, running, source
        try:
            while True:
                msg = await ws.receive_json()
                cmd = msg.get("cmd")
                if cmd == "config":
                    mac = msg.get("mac", "")
                    use_sim = SIM_DEFAULT or msg.get("sim", False)
                elif cmd == "start":
                    if source is None:
                        try:
                            source = SimSource() if use_sim else BITalinoSource(mac)
                        except Exception as e:
                            await ws.send_json({"error": f"connect failed: {e}"})
                            continue
                    running = True
                elif cmd == "stop":
                    running = False
                elif cmd == "close":
                    running = False
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass

    recv_task = asyncio.create_task(recv_loop())
    try:
        while True:
            if recv_task.done() and ws.client_state != WebSocketState.CONNECTED:
                break
            if running and source is not None:
                try:
                    raw = await asyncio.to_thread(source.read_window)
                except Exception as e:
                    await ws.send_json({"error": f"read failed: {e}"})
                    running = False
                    continue
                await ws.send_json(session.process(raw))
            else:
                await asyncio.sleep(0.03)
    except WebSocketDisconnect:
        pass
    finally:
        running = False
        recv_task.cancel()
        if source is not None:
            source.close()


# Serve the front-end so a single command runs the whole thing.
_web = Path(__file__).resolve().parent.parent
if _web.is_dir():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")
