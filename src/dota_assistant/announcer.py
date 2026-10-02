"""Cross-platform voice output with a queue so alerts never overlap.

Backends by OS:
  - macOS:   built-in `say` command
  - Windows: pyttsx3 (SAPI5); falls back to PowerShell System.Speech
  - Linux:   espeak if installed

Set env DOTA_ASSISTANT_MUTE=1 to print announcements without speaking.
"""

import os
import platform
import queue
import subprocess
import threading


class Announcer:
    def __init__(self, voice: str = "", rate: int = 210):
        self.voice = voice
        self.rate = rate  # words per minute
        self.system = platform.system()
        self.muted = os.environ.get("DOTA_ASSISTANT_MUTE") == "1"
        self.sinks: list = []  # extra outputs (e.g. Discord voice), called per alert
        # Optional predicate: when it returns True, skip local OS speech (e.g.
        # because Discord voice is live and would otherwise double up). Local
        # speech resumes automatically whenever it returns False, so it still
        # acts as a fallback if the sink isn't connected.
        self.suppress_local = None
        self._queue: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text: str) -> None:
        print(f"  🔊 {text}", flush=True)
        suppressed = bool(self.suppress_local and self.suppress_local())
        if not self.muted and not suppressed:
            self._queue.put(text)
        for sink in self.sinks:
            try:
                sink(text)
            except Exception as exc:
                print(f"  (sink failed: {exc})", flush=True)

    # --- worker -------------------------------------------------------------

    def _worker(self) -> None:
        speak = self._make_speaker()
        failures = 0
        while True:
            text = self._queue.get()
            try:
                speak(text)
                failures = 0
            except Exception as exc:
                failures += 1
                print(f"  (speech failed: {exc})", flush=True)
                if failures >= 3:
                    # The engine may be wedged (e.g. a dead SAPI COM object) —
                    # rebuild it and keep going rather than giving up for good.
                    print("  (re-initialising speech engine)", flush=True)
                    speak = self._make_speaker()
                    failures = 0

    def _make_speaker(self):
        if self.system == "Darwin":
            return self._speak_macos
        if self.system == "Windows":
            engine = self._init_pyttsx3()
            if engine is not None:
                return lambda text: self._speak_pyttsx3(engine, text)
            print("  (pyttsx3 unavailable — using PowerShell speech)", flush=True)
            return self._speak_powershell
        return self._speak_espeak  # Linux / other

    # --- macOS ----------------------------------------------------------------

    def _speak_macos(self, text: str) -> None:
        cmd = ["say", "-r", str(self.rate)]
        if self.voice:
            cmd += ["-v", self.voice]
        subprocess.run(cmd + [text], check=False)

    # --- Windows: pyttsx3 (preferred) -------------------------------------

    def _init_pyttsx3(self):
        try:
            import pyttsx3

            engine = pyttsx3.init()
            engine.setProperty("rate", self.rate)
            if self.voice:
                for v in engine.getProperty("voices"):
                    if self.voice.lower() in v.name.lower():
                        engine.setProperty("voice", v.id)
                        break
            return engine
        except Exception:
            return None

    @staticmethod
    def _speak_pyttsx3(engine, text: str) -> None:
        engine.say(text)
        engine.runAndWait()

    # --- Windows: PowerShell fallback --------------------------------------

    def _speak_powershell(self, text: str) -> None:
        # SAPI rate is -10..10; map from words per minute (~200 wpm = 0).
        sapi_rate = max(-10, min(10, round((self.rate - 200) / 25)))
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {sapi_rate}; "
            "$s.Speak([Console]::In.ReadToEnd())"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            input=text.encode(),
            check=False,
        )

    # --- Linux --------------------------------------------------------------

    def _speak_espeak(self, text: str) -> None:
        subprocess.run(["espeak", "-s", str(self.rate), text], check=False)
