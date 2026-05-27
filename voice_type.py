"""
Voice-to-paste with tray icon.
- Hotkeys: F12 (laptop) or Pause/Break (external keyboard)
- Tray icon shows state: white=idle, green=recording, yellow=transcribing, red=error
- Pastes transcribed text into whatever window has focus
- No audio cues
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw
from pynput import keyboard

import pystray
from pystray import MenuItem as Item

# --- Config -----------------------------------------------------------------
SAMPLE_RATE = 16000
CHANNELS = 1
INPUT_DEVICE = 6               # 6 = system default (resamples to 16kHz); 3 = Sennheiser headset
OUTPUT_PATH = Path("/tmp/voice-type.wav")
HOTKEYS = ["<f12>", "<pause>"]

MODEL_SIZE = "small.en"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
BEAM_SIZE = 1

ERROR_CLEAR_SECONDS = 3        # how long to show red before going back to white
HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thanks for watching!",
    ". . .", "...", ".", "bye", "bye.", "okay.", "ok.",
}
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def make_icon(color: str) -> Image.Image:
    """Generate a 64x64 colored circle icon with a small mic glyph."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    fill_colors = {
        "white":  (240, 240, 240, 255),
        "green":  (60, 200, 90, 255),
        "yellow": (240, 200, 50, 255),
        "red":    (220, 70, 70, 255),
    }
    border_colors = {
        "white":  (100, 100, 100, 255),
        "green":  (30, 130, 60, 255),
        "yellow": (180, 140, 30, 255),
        "red":    (160, 40, 40, 255),
    }

    fill = fill_colors[color]
    border = border_colors[color]

    # Outer circle
    draw.ellipse((4, 4, size - 4, size - 4), fill=fill, outline=border, width=2)

    # Mic glyph (capsule + stand)
    cx, cy = size // 2, size // 2 - 4
    cap_w, cap_h = 14, 22
    glyph = (50, 50, 50, 255) if color in ("white", "yellow") else (255, 255, 255, 255)
    draw.rounded_rectangle(
        (cx - cap_w // 2, cy - cap_h // 2, cx + cap_w // 2, cy + cap_h // 2),
        radius=7, fill=glyph,
    )
    # U-shaped base
    draw.arc((cx - 14, cy - 4, cx + 14, cy + 18), start=0, end=180, fill=glyph, width=3)
    # Vertical stand
    draw.line((cx, cy + 16, cx, cy + 22), fill=glyph, width=3)
    return img


class TrayIcon:
    def __init__(self, on_quit):
        self._icons = {c: make_icon(c) for c in ("white", "green", "yellow", "red")}
        self._on_quit = on_quit
        self.icon = pystray.Icon(
            "voice-type",
            self._icons["white"],
            "Voice Type - idle",
            menu=pystray.Menu(
                Item("Voice Type", None, enabled=False),
                Item("Quit", self._quit),
            ),
        )
        self._error_timer = None

    def _quit(self, icon, item):
        self._on_quit()
        icon.stop()

    def set_state(self, state: str, tooltip: str = ""):
        if state not in self._icons:
            return
        self.icon.icon = self._icons[state]
        self.icon.title = tooltip or f"Voice Type - {state}"

    def show_error(self, message: str):
        self.set_state("red", f"Voice Type - error: {message}")
        if self._error_timer:
            self._error_timer.cancel()
        self._error_timer = threading.Timer(
            ERROR_CLEAR_SECONDS, lambda: self.set_state("white")
        )
        self._error_timer.daemon = True
        self._error_timer.start()

    def run(self):
        self.icon.run()  # blocks


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.is_recording = False
        self.frames = []
        self.audio_queue = queue.Queue()
        self.stream = None
        self.writer_thread = None

    def _audio_callback(self, indata, frames, time_info, status):
        self.audio_queue.put(indata.copy())

    def _drain_queue(self):
        while self.is_recording or not self.audio_queue.empty():
            try:
                self.frames.append(self.audio_queue.get(timeout=0.1))
            except queue.Empty:
                continue

    def start(self):
        if self.is_recording:
            return
        self.frames = []
        self.audio_queue = queue.Queue()
        self.is_recording = True
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            device=INPUT_DEVICE, callback=self._audio_callback,
        )
        self.stream.start()
        self.writer_thread = threading.Thread(target=self._drain_queue, daemon=True)
        self.writer_thread.start()

    def stop(self):
        if not self.is_recording:
            return None
        self.is_recording = False
        self.stream.stop()
        self.stream.close()
        self.writer_thread.join()
        if not self.frames:
            return None

        audio_int16 = np.concatenate(self.frames, axis=0).flatten()
        with wave.open(str(OUTPUT_PATH), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())
        return audio_int16.astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def is_hallucination(text: str) -> bool:
    return text.lower().strip(" .!?") in HALLUCINATIONS


def transcribe(model: WhisperModel, audio: np.ndarray) -> str:
    if audio is None or len(audio) < SAMPLE_RATE * 0.3:
        return ""
    segments, _ = model.transcribe(audio, beam_size=BEAM_SIZE, vad_filter=False)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if is_hallucination(text):
        return ""
    return text


# ---------------------------------------------------------------------------
# Pasting
# ---------------------------------------------------------------------------
def paste_text(text: str):
    """Set clipboard to text, then simulate Ctrl+Shift+V (terminal paste)."""
    if not text:
        return

    # Save current clipboard
    try:
        old = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True, text=True, timeout=1,
        ).stdout
    except Exception:
        old = ""

    # Set new clipboard with trailing space (so multiple dictations chain nicely)
    subprocess.run(
        ["xclip", "-selection", "clipboard"],
        input=text + " ", text=True, check=True,
    )

    # Small delay so the clipboard is definitely set
    time.sleep(0.05)

    # Paste with Ctrl+Shift+V (works in terminals)
    subprocess.run(["xdotool", "key", "ctrl+shift+v"], check=True)

    # Restore the previous clipboard after a delay
    def _restore():
        time.sleep(0.5)
        try:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=old, text=True, check=False,
            )
        except Exception:
            pass
    threading.Thread(target=_restore, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Sanity checks
    for tool in ("xdotool", "xclip"):
        if shutil.which(tool) is None:
            print(f"[!] Missing dependency: install with `sudo apt install {tool}`")
            sys.exit(1)

    info = sd.query_devices(INPUT_DEVICE)
    print(f"[mic] Using device {INPUT_DEVICE}: {info['name']}")

    print(f"[...] Loading {MODEL_SIZE} model...")
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    print("[...] Warming up...")
    list(model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), beam_size=1)[0])
    print("[✓] Ready.")

    recorder = Recorder()
    lock = threading.Lock()

    # Tray must run on the main thread on Linux; workers run on background threads.
    tray = TrayIcon(on_quit=lambda: os._exit(0))

    def handle_toggle():
        with lock:
            if recorder.is_recording:
                # Stop -> transcribing -> paste
                tray.set_state("yellow", "Voice Type - transcribing...")
                audio = recorder.stop()
                try:
                    text = transcribe(model, audio)
                    if text:
                        print(f"📝 {text}")
                        paste_text(text)
                        tray.set_state("white")
                    else:
                        tray.show_error("empty")
                except Exception as e:
                    print(f"[!] transcription failed: {e}")
                    tray.show_error("transcription")
            else:
                # Start recording
                try:
                    recorder.start()
                    tray.set_state("green", "Voice Type - recording...")
                except Exception as e:
                    print(f"[!] recording failed: {e}")
                    tray.show_error("mic")

    def on_toggle():
        threading.Thread(target=handle_toggle, daemon=True).start()

    # Hotkey listener in a background thread
    def hotkey_loop():
        bindings = {hk: on_toggle for hk in HOTKEYS}
        with keyboard.GlobalHotKeys(bindings):
            threading.Event().wait()

    threading.Thread(target=hotkey_loop, daemon=True).start()
    print(f"[ready] Hotkeys: {' or '.join(HOTKEYS)}  |  Right-click tray icon to quit\n")

    # Tray must run on main thread - this blocks
    tray.run()


if __name__ == "__main__":
    main()