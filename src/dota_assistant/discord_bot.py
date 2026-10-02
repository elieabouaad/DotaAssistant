"""Discord voice announcer: a bot joins your voice channel and speaks every
announcement there (in addition to local TTS).

Setup (see README):
  1. https://discord.com/developers/applications → New Application → Bot →
     copy the token.
  2. Invite the bot to your server with Connect + Speak permissions.
  3. In Discord, enable Developer Mode, right-click your voice channel →
     Copy Channel ID.
  4. Fill [discord] in config.toml (token can also come from the
     DISCORD_BOT_TOKEN env var).
  5. FFmpeg must be installed and on PATH (audio playback).

Speech is synthesized with edge-tts (free Microsoft neural voices, needs
internet). Announcements queue so they never talk over each other.
"""

import asyncio
import os
import shutil
import tempfile
import threading


class DiscordAnnouncer:
    def __init__(self, cfg: dict):
        self.token = cfg.get("bot_token") or os.environ.get("DISCORD_BOT_TOKEN", "")
        self.channel_id = int(cfg.get("voice_channel_id") or 0)
        self.voice = cfg.get("voice", "en-US-GuyNeural")
        self.enabled = bool(cfg.get("enabled")) and bool(self.token) and self.channel_id
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.ready = threading.Event()

        if not self.enabled:
            return
        if not shutil.which("ffmpeg"):
            print("Discord announcer disabled: ffmpeg not found on PATH", flush=True)
            self.enabled = False
            return
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text: str) -> None:
        """Thread-safe; called from the announcer for every spoken alert."""
        if self.enabled and self.ready.is_set():
            asyncio.run_coroutine_threadsafe(self.queue.put(text), self.loop)

    # --- bot thread -----------------------------------------------------------

    def _run(self) -> None:
        try:
            import discord
            import edge_tts
        except ImportError as exc:
            print(f"Discord announcer disabled: {exc}", flush=True)
            self.enabled = False
            return

        client = discord.Client(intents=discord.Intents.default())

        async def speaker(vc, channel):
            while True:
                text = await self.queue.get()
                try:
                    if not vc.is_connected():
                        vc = await channel.connect()
                    fd, path = tempfile.mkstemp(suffix=".mp3")
                    os.close(fd)
                    await edge_tts.Communicate(text, self.voice).save(path)
                    done = asyncio.Event()
                    vc.play(
                        discord.FFmpegPCMAudio(path),
                        after=lambda _e: client.loop.call_soon_threadsafe(done.set),
                    )
                    await done.wait()
                    os.unlink(path)
                except Exception as exc:
                    print(f"Discord speech error: {exc}", flush=True)

        @client.event
        async def on_ready():
            if self.ready.is_set():  # reconnect after a network blip
                return
            self.loop = asyncio.get_running_loop()
            self.queue = asyncio.Queue()
            try:
                channel = client.get_channel(self.channel_id) or await client.fetch_channel(self.channel_id)
                vc = await channel.connect()
            except Exception as exc:
                print(f"Discord: could not join voice channel {self.channel_id}: {exc}", flush=True)
                self.enabled = False
                return
            print(f"Discord: joined voice channel '{channel.name}'", flush=True)
            self.ready.set()
            client.loop.create_task(speaker(vc, channel))

        try:
            client.run(self.token, log_handler=None)
        except Exception as exc:
            print(f"Discord announcer failed: {exc}", flush=True)
            self.enabled = False
