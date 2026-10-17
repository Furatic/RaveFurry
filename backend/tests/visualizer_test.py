"""Optional visualizer tests: signal accuracy, native cleanup, and local transport."""
import asyncio
import importlib.util
import math
import socket
import sys
import time
import types
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(m) is not None for m in ("numpy", "aiohttp"))
if AVAILABLE:
    import numpy as np
    from aiohttp import ClientSession, WSServerHandshakeError, web
    from visualizer.dsp import Analyzer, RhythmContext, empty_frame
    from visualizer.capture import Capture
    from visualizer.service import create_app
    from visualizer.service import ParentGuard


@unittest.skipUnless(AVAILABLE, "Install the optional visualizer extra")
class SignalTests(unittest.TestCase):
    def passage(self, build, seconds=6, rate=48000):
        analyzer = Analyzer(rate, 2)
        frames = []
        for i in range(round(seconds * rate / 512)):
            t = (np.arange(512) + i * 512) / rate
            wave = build(t)
            frame = analyzer.feed(np.column_stack([wave, -wave]).astype("<f4").tobytes(), 100 + i * 512 / rate)
            if frame is not None:
                frames.append(frame)
        return analyzer, frames

    def tone(self, frequency, amplitude=.3, antiphase=False):
        analyzer = Analyzer()
        for i in range(24):
            t = (np.arange(512) + i * 512) / 48000
            wave = np.sin(t * math.tau * frequency) * amplitude
            stereo = np.column_stack([wave, -wave if antiphase else wave]).astype("<f4")
            analyzer.feed(stereo.tobytes(), 100 + i * 512 / 48000)
        return analyzer.frame

    def test_stereo_energy_survives_antiphase(self):
        frame = self.tone(80, .4, True)
        self.assertGreater(frame["rms"][0], .25)
        self.assertGreater(frame["bass"], .8)
        self.assertAlmostEqual(frame["rms"][0], frame["rms"][1], places=5)

    def test_frequency_separation_and_level(self):
        bass, mid, high = [self.tone(f) for f in (80, 1000, 8000)]
        self.assertGreater(bass["bass"], bass["mid"])
        self.assertGreater(mid["mid"], mid["bass"])
        self.assertGreater(high["treble"], high["mid"])
        self.assertGreater(self.tone(1000, .4)["level"], self.tone(1000, .003)["level"])
        self.assertEqual(len(bass["bands"]), 48)

    def test_silence_and_bounded_buffer(self):
        analyzer = Analyzer()
        for i in range(200):
            analyzer.feed(np.zeros((512, 2), dtype="<f4").tobytes(), 100 + i / 60)
        self.assertEqual(analyzer.frame["level"], 0)
        self.assertEqual(analyzer.frame["kickId"], 0)
        self.assertTrue(all(v == 0 for v in analyzer.frame["bands"]))
        self.assertEqual(analyzer.samples.shape, (2048, 2))

    def test_kicks_are_transients_not_a_fixed_tempo(self):
        analyzer = Analyzer()
        for i in range(300):
            t = (np.arange(512) + i * 512) / 48000
            phase = t % .5
            envelope = (1 - np.exp(-phase / .003)) * np.exp(-phase / .070)
            wave = np.sin(t * math.tau * 80) * envelope * .65
            analyzer.feed(np.column_stack([wave, wave]).astype("<f4").tobytes(), 100 + i * 512 / 48000)
        self.assertGreaterEqual(analyzer.frame["kickId"], 5)
        self.assertLessEqual(analyzer.frame["kickId"], 10)

    def test_recovery_ids_do_not_accumulate_offsets_per_frame(self):
        capture = Capture()
        capture.kick_offset, capture.accent_offset = 100, 50
        frame = empty_frame();frame["kickId"] = 1;frame["accentId"] = 2
        capture._publish(frame);capture._publish(frame)
        self.assertEqual(capture.frame["kickId"], 101)
        self.assertEqual(capture.frame["accentId"], 52)
        self.assertEqual(frame["kickId"], 1)
        capture.frame["capturedAt"] = time.monotonic() - 5
        capture.status = "capturing"
        capture.frame["level"] = .8
        self.assertEqual(capture.snapshot()["level"], 0)
        self.assertEqual(capture.snapshot()["status"], "idle")

    def test_each_cloud_has_its_own_spectral_energy_and_onsets(self):
        for cloud, frequency in enumerate((80, 1100, 350, 5000)):
            analyzer, _ = self.passage(lambda t: .25 * np.sin(t * math.tau * frequency), seconds=3)
            frame = analyzer.frame
            self.assertEqual(frame["cloudIds"][cloud], 1)
            self.assertEqual(sum(frame["cloudIds"]), 1, (frequency, frame["cloudIds"]))
            self.assertGreater(frame["cloudLevels"][cloud], .8)
            self.assertGreater(frame["cloudBalance"][cloud], .98)

    def test_fast_mixed_compressed_music_retains_independent_hits(self):
        def mixed(t):
            phase = t % .25
            envelope = (1 - np.exp(-phase / .002)) * np.exp(-phase / .035)
            wave = .22 * np.sin(t * math.tau * 1050)
            for frequency, strength in ((75, .23), (350, .12), (6200, .14)):
                wave += strength * np.sin(t * math.tau * frequency) * envelope
            return np.tanh(wave * 2) * .45
        analyzer, _ = self.passage(mixed)
        for cloud in (0, 2, 3):
            self.assertGreaterEqual(analyzer.frame["cloudIds"][cloud], 20)
            self.assertLessEqual(analyzer.frame["cloudIds"][cloud], 26)
        self.assertGreater(analyzer.frame["cloudLevels"][1], .5)

    def test_sustained_vocal_vibrato_does_not_invent_bass_drums(self):
        def vocal(t):
            phase = math.tau * (220 * t - 4 / (math.tau * 5) * np.cos(t * math.tau * 5))
            return .12 * np.sin(phase) + .10 * np.sin(phase * 3) + .07 * np.sin(phase * 6) + .02 * np.sin(phase * 13)
        analyzer, frames = self.passage(vocal)
        self.assertLessEqual(analyzer.frame["cloudIds"][0], 1)
        self.assertLessEqual(max(analyzer.frame["cloudIds"]), 4)
        self.assertGreater(analyzer.frame["cloudLevels"][1], .5)
        self.assertGreater(analyzer.frame["cloudLevels"][2], .5)
        self.assertGreater(np.ptp([f["cloudLevels"][1] for f in frames]), .1)

    def test_slow_swells_and_quiet_noise_are_not_repetitive_beats(self):
        def pad(t):
            envelope = (.3 + .25 * np.sin(t * .7)) * (1 - np.exp(-t / 2))
            return envelope * sum(.08 * np.sin(t * math.tau * f) for f in (80, 350, 1000, 6000))
        analyzer, frames = self.passage(pad, seconds=8)
        self.assertLessEqual(max(analyzer.frame["cloudIds"]), 2)
        self.assertGreater(np.ptp([f["cloudLevels"][1] for f in frames]), .3)
        rng = np.random.default_rng(41)
        noise, _ = self.passage(lambda t: rng.normal(0, 1e-6, len(t)))
        self.assertEqual(noise.frame["cloudIds"], [0] * 4)
        self.assertEqual(noise.frame["cloudLevels"], [0.0] * 4)

    def test_cloud_recovery_offsets_stale_fields_and_immutable_snapshots(self):
        capture = Capture()
        capture.cloud_offsets = [10, 20, 30, 40]
        frame = empty_frame()
        frame["cloudIds"] = [1, 2, 3, 4]
        frame["cloudLevels"] = [.7] * 4
        frame.update(rhythmDrive=.8, rhythmConfidence=.9, rhythmTempo=120)
        capture._publish(frame)
        capture._publish(frame)
        self.assertEqual(capture.frame["cloudIds"], [11, 22, 33, 44])
        self.assertEqual(frame["cloudIds"], [1, 2, 3, 4])
        capture.frame["capturedAt"] = time.monotonic() - 5
        snapshot = capture.snapshot()
        for name in ("cloudLevels", "cloudBalance", "cloudStrength", "cloudFlux"):
            self.assertEqual(snapshot[name], [0.0] * 4)
        self.assertEqual(capture.frame["cloudLevels"], [.7] * 4)
        self.assertEqual(snapshot["cloudIds"], [11, 22, 33, 44])
        for name in ("rhythmDrive", "rhythmConfidence", "rhythmTempo"):
            self.assertEqual(snapshot[name], 0)
        self.assertEqual(capture.frame["rhythmTempo"], 120)

    def test_formats_and_corrupt_samples_remain_finite_and_bounded(self):
        for rate, channels in ((44100, 2), (48000, 1), (8000, 2), (192000, 8)):
            analyzer = Analyzer(rate, channels)
            data = np.full((4096, channels), np.nan, dtype="<f4")
            data[0] = np.inf
            frame = analyzer.feed(data.tobytes(), 100)
            self.assertTrue(all(math.isfinite(v) for v in frame["cloudLevels"]))
            self.assertEqual(analyzer.samples.shape, (2048, channels))

    def test_wider_vocal_vibrato_remains_continuous_not_a_strobe(self):
        def vocal(t):
            phase = math.tau * (247 * t - 8 / (math.tau * 6.1) * np.cos(t * math.tau * 6.1))
            return sum(a * np.sin(phase * k) for k, a in ((1,.11),(2,.06),(4,.08),(7,.035),(13,.015),(20,.008)))
        analyzer, frames = self.passage(vocal, seconds=10)
        self.assertLessEqual(max(analyzer.frame["cloudIds"]), 2)
        self.assertEqual(analyzer.frame["rhythmTempo"], 0)
        self.assertGreater(max(f["cloudLevels"][1] for f in frames), .6)

    def test_sung_envelope_does_not_trigger_faster_than_its_phrasing(self):
        def vocal(t):
            phase = math.tau * (247 * t - 8 / (math.tau * 6.1) * np.cos(t * math.tau * 6.1))
            harmonics = sum(a * np.sin(phase * k) for k,a in ((1,.11),(2,.06),(4,.08),(7,.035),(13,.015),(20,.008)))
            return harmonics * (.55 + .4 * np.sin(t * math.tau * 1.7) ** 2)
        analyzer, frames = self.passage(vocal, seconds=8)
        # The 3.4 Hz energy phrasing, not its 6.1 Hz pitch vibrato, supplies
        # the attacks. Harmonics sharing an attack receive one coherent cue.
        self.assertEqual(analyzer.frame["cloudIds"][0], 0)
        self.assertLessEqual(max(analyzer.frame["cloudIds"]), 29)
        at = [f["cloudAt"][1] for f in frames if f["cloudAt"][1]]
        events = np.unique(at)
        if len(events) > 4:
            self.assertGreater(float(np.median(np.diff(events))), .26)

    def test_dense_vocal_mix_keeps_steady_kicks_in_sync(self):
        def mix(t):
            phase = math.tau * (247 * t - 8 / (math.tau * 6.1) * np.cos(t * math.tau * 6.1))
            vocal = sum(a * np.sin(phase * k) for k,a in ((1,.11),(2,.06),(4,.08),(7,.035),(13,.015)))
            vocal *= .55 + .4 * np.sin(t * math.tau * 1.7) ** 2
            p = t % .5
            kick = .23 * np.sin(t * math.tau * 75) * (1 - np.exp(-p/.002)) * np.exp(-p/.045)
            pad = .08 * np.sin(t * math.tau * 85) + .07 * np.sin(t * math.tau * 370)
            return np.tanh(2 * (vocal + kick + pad)) * .4
        analyzer, frames = self.passage(mix, seconds=10)
        times = np.unique([f["cloudAt"][0] - 100 for f in frames if f["cloudAt"][0]])
        beats = np.arange(.5, 9.9, .5)
        aligned = sum(np.min(abs(times - beat)) < .09 for beat in beats)
        self.assertGreaterEqual(aligned, len(beats) - 2)
        self.assertLessEqual(len(times), 24)
        self.assertGreater(analyzer.frame["rhythmConfidence"], .4)

    def test_silence_does_not_continue_a_learned_rhythm(self):
        def passage(t):
            p = t % .5
            wave = .4 * np.sin(t * math.tau * 80) * (1 - np.exp(-p/.002)) * np.exp(-p/.045)
            return np.where(t < 6, wave, 0)
        analyzer, frames = self.passage(passage, seconds=9)
        before = next(f for f in frames if f["capturedAt"] > 106.3)
        self.assertEqual(before["cloudIds"], analyzer.frame["cloudIds"])
        self.assertEqual(analyzer.frame["rhythmConfidence"], 0)
        self.assertEqual(analyzer.frame["rhythmDrive"], 0)

    def test_rhythm_history_is_fixed_and_recovers_from_clock_gaps(self):
        rhythm = RhythmContext()
        history = rhythm.history
        for i in range(4000):
            novelty = np.array([.65 if i % 25 == 0 else 0, 0, 0, 0])
            rhythm.observe(novelty, 100 + i / 50)
        self.assertIs(rhythm.history, history)
        self.assertEqual(history.shape, (400, 4))
        self.assertEqual(history.nbytes, 6400)
        self.assertEqual(rhythm.count, 400)
        self.assertGreater(rhythm.confidence, .4)
        self.assertAlmostEqual(rhythm.period, .5, delta=.025)
        rhythm.observe(np.zeros(4), 190)
        self.assertEqual(rhythm.confidence, 0)
        self.assertLess(rhythm.count, 3)
        rhythm.observe(np.zeros(4), 189)  # clock reversal must also rebase
        self.assertEqual(rhythm.confidence, 0)
        self.assertLess(rhythm.count, 3)

    def test_plucked_and_soft_harmonic_attacks_follow_notes(self):
        # Analytic instrument-like envelopes, not recorded piano/guitar/brass.
        # They exercise the important difference between a sharp pluck and a
        # softer harmonic attack without adding audio assets or dependencies.
        for attack, decay in ((.005,.45), (.012,.22), (.090,.65)):
            def phrase(t):
                p = t % 1.0
                env = (1 - np.exp(-p / attack)) * np.exp(-p / decay)
                pitch = np.take([261.63,329.63,392,293.66], np.floor(t).astype(int) % 4)
                phase = math.tau * pitch * t + .03 * np.sin(t * math.tau * 5)
                return .20 * env * sum(np.sin(phase * k) / k ** 1.4 for k in range(1,7))
            analyzer, frames = self.passage(phrase, seconds=8)
            self.assertGreaterEqual(max(analyzer.frame["cloudIds"]), 6)
            self.assertLessEqual(max(analyzer.frame["cloudIds"]), 12)
            actual = np.unique([at - 100 for f in frames for at in f["cloudAt"] if at])
            for note in range(1,8):
                self.assertLess(float(np.min(abs(actual - note))), .22)

    def test_legato_timbre_shapes_energy_without_random_flashes(self):
        def phrase(t):
            blend = .5 + .45 * np.sin(t * .55)
            return (.12 * np.sin(t * math.tau * 330)
                    + .10 * blend * np.sin(t * math.tau * 1320)
                    + .10 * (1 - blend) * np.sin(t * math.tau * 3960))
        analyzer, frames = self.passage(phrase, seconds=10)
        self.assertLessEqual(max(analyzer.frame["cloudIds"]), 3)
        self.assertGreater(np.ptp([f["cloudLevels"][3] for f in frames]), .2)

    def test_strong_syncopation_is_not_forced_onto_the_learned_grid(self):
        notes = np.array([.0,.5,1,1.5,2,2.5,3,3.5,4,4.25,4.75,5.25,6,6.5,6.75,7.5])
        def phrase(t):
            age = t[:, None] - notes
            env = np.where(age >= 0, (1 - np.exp(-np.maximum(0,age)/.003)) * np.exp(-np.maximum(0,age)/.050), 0).sum(axis=1)
            return .25 * np.sin(t * math.tau * 1000) * env
        analyzer, frames = self.passage(phrase, seconds=8)
        actual = np.unique([f["cloudAt"][1] - 100 for f in frames if f["cloudAt"][1]])
        self.assertGreaterEqual(sum(np.min(abs(actual - note)) < .09 for note in notes), len(notes) - 2)
        self.assertLessEqual(analyzer.frame["cloudIds"][1], len(notes) + 1)


@unittest.skipUnless(AVAILABLE, "Install the optional visualizer extra")
class NativeOwnershipTests(unittest.TestCase):
    def test_idle_capture_is_not_reopened_and_closes_native_objects(self):
        capture = Capture()
        capture.kick_offset = capture.accent_offset = 0
        owned = {"manager": 0, "stream": 0, "opened": 0}
        class Stream:
            def __enter__(self): owned["stream"] += 1;return self
            def __exit__(self, *args): owned["stream"] -= 1
            def is_active(self): capture.stop_event.set();return True
        class Manager:
            def __enter__(self): owned["manager"] += 1;return self
            def __exit__(self, *args): owned["manager"] -= 1
            def get_default_wasapi_loopback(self): return {"maxInputChannels": 2, "defaultSampleRate": 48000, "index": 1, "name": "test"}
            def open(self, **kwargs):
                assert kwargs["input"] and not kwargs["output"]
                owned["opened"] += 1;return Stream()
        module = types.SimpleNamespace(PyAudio=Manager, paFloat32=1, paContinue=0, paComplete=1)
        with patch.dict(sys.modules, {"pyaudiowpatch": module}): capture._windows()
        self.assertEqual(owned, {"manager": 0, "stream": 0, "opened": 1})

    def test_native_device_failure_closes_stream_and_manager(self):
        capture = Capture()
        owned = []
        class Stream:
            def __enter__(self): return self
            def __exit__(self, *args): owned.append("stream closed")
            def is_active(self): raise RuntimeError("device disconnected")
        class Manager:
            def __enter__(self): return self
            def __exit__(self, *args): owned.append("manager closed")
            def get_default_wasapi_loopback(self):
                return {"maxInputChannels": 2, "defaultSampleRate": 48000, "index": 1, "name": "test"}
            def open(self, **kwargs): return Stream()
        module = types.SimpleNamespace(PyAudio=Manager, paFloat32=1, paContinue=0, paComplete=1)
        with patch.dict(sys.modules, {"pyaudiowpatch": module}):
            with self.assertRaisesRegex(RuntimeError, "disconnected"): capture._windows()
        self.assertEqual(owned, ["stream closed", "manager closed"])

    def test_silence_is_healthy_but_a_stalled_worker_is_not(self):
        capture = Capture()
        capture.thread = types.SimpleNamespace(is_alive=lambda: True)
        capture.heartbeat = time.monotonic()
        capture.frame["capturedAt"] = time.monotonic() - 1000
        capture.status = "capturing"
        self.assertEqual(capture.snapshot()["status"], "idle")
        self.assertTrue(capture.healthy())
        capture.heartbeat -= 25
        self.assertFalse(capture.healthy())

    def test_windows_parent_handle_is_owned_once_and_closed_once(self):
        import visualizer.service as service
        calls = {"open": 0, "close": 0}
        class Function:
            def __init__(self, callback): self.callback = callback
            def __call__(self, *args): return self.callback(*args)
        def opened(*args): calls["open"] += 1;return 42
        def closed(*args): calls["close"] += 1;return 1
        kernel = types.SimpleNamespace(OpenProcess=Function(opened),
            WaitForSingleObject=Function(lambda *args: 0x102), CloseHandle=Function(closed))
        with patch.object(service.os, "name", "nt"), patch.object(service.ctypes, "WinDLL", return_value=kernel, create=True):
            guard = ParentGuard(123)
            for _ in range(1000): self.assertTrue(guard.alive())
            guard.close();guard.close()
        self.assertEqual(calls, {"open": 1, "close": 1})

    def test_linux_capture_closes_monitor_on_failure(self):
        import visualizer.capture as module
        capture = Capture()
        calls = []
        pipe = types.SimpleNamespace(close=lambda: calls.append("pipe closed"))
        process = types.SimpleNamespace(stdout=pipe, poll=lambda: None,
            terminate=lambda: calls.append("terminated"), wait=lambda **kwargs: calls.append("waited"))
        with patch.object(module.shutil, "which", return_value="/usr/bin/parec"), \
                patch.object(module.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(module.select, "select", side_effect=RuntimeError("monitor failed")):
            with self.assertRaisesRegex(RuntimeError, "monitor failed"): capture._linux()
        self.assertEqual(calls, ["terminated", "waited", "pipe closed"])
        self.assertIn("--latency-msec=20", spawn.call_args.args[0])

    def test_windows_shutdown_targets_only_the_owned_child(self):
        import visualizer.supervisor as module
        companion = module.VisualizerCompanion(".")
        calls = []
        companion.child = types.SimpleNamespace(poll=lambda: None,
            send_signal=lambda signum: calls.append(("signal", signum)),
            wait=lambda **kwargs: calls.append(("wait", kwargs["timeout"])))
        with patch.object(module.os, "name", "nt"), patch.object(module.signal, "CTRL_BREAK_EVENT", 1, create=True):
            companion._stop_child()
        self.assertEqual(calls, [("signal", 1), ("wait", 3)])
        self.assertIsNone(companion.child)

    def test_windows_without_console_still_cleans_up_owned_child(self):
        import visualizer.supervisor as module
        companion = module.VisualizerCompanion(".")
        calls = []
        def unavailable(signum): raise OSError("no console")
        companion.child = types.SimpleNamespace(poll=lambda: None, send_signal=unavailable,
            terminate=lambda: calls.append("terminated"), wait=lambda **kwargs: calls.append("waited"))
        with patch.object(module.os, "name", "nt"), patch.object(module.signal, "CTRL_BREAK_EVENT", 1, create=True):
            companion._stop_child()
        self.assertEqual(calls, ["terminated", "waited"])


@unittest.skipUnless(AVAILABLE, "Install the optional visualizer extra")
class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = socket.socket();temporary.bind(("127.0.0.1", 0))
        self.port = temporary.getsockname()[1];temporary.close()
        fake_thread = types.SimpleNamespace(is_alive=lambda: True)
        self.capture = types.SimpleNamespace(thread=fake_thread, healthy=lambda: True, snapshot=lambda: {**empty_frame(), "serverTime": time.monotonic(), "status": "idle", "device": "test", "error": ""})
        self.runner = web.AppRunner(create_app(self.capture, self.port), access_log=None, shutdown_timeout=1)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        self.client = ClientSession()
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_served_page_health_and_forbidden_origin(self):
        async with self.client.get(self.base + "/obs-background.html") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("u_clouds[4]", await response.text())
        async with self.client.get(self.base + "/_healthz/") as response:
            self.assertEqual(response.status, 200)
        async with self.client.get(self.base + "/", headers={"Host": "public.example"}) as response:
            self.assertEqual(response.status, 403)
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect(self.base + "/features", origin="https://public.example")

    async def test_socket_cap_reconnect_and_metadata_only(self):
        sockets = []
        try:
            for i in range(4):
                ws = await self.client.ws_connect(self.base + "/features", origin="null")
                data = await asyncio.wait_for(ws.receive_json(), 1)
                self.assertEqual(len(data["bands"]), 48)
                self.assertNotIn("audio", data)
                sockets.append(ws)
            with self.assertRaises(WSServerHandshakeError):
                await self.client.ws_connect(self.base + "/features")
            await sockets.pop().close()
            await asyncio.sleep(.02)
            ws = await self.client.ws_connect(self.base + "/features")
            self.assertEqual((await ws.receive_json())["version"], 1)
            sockets.append(ws)
        finally:
            for ws in sockets: await ws.close()

    async def test_socket_rejects_commands_and_health_detects_stall(self):
        from aiohttp import WSMsgType
        ws = await self.client.ws_connect(self.base + "/features")
        await ws.receive_json()
        await ws.send_str("restart playback")
        for _ in range(5):
            message = await asyncio.wait_for(ws.receive(), 1)
            if message.type == WSMsgType.CLOSE: break
        self.assertEqual(ws.close_code, 1008)
        await ws.close()
        self.capture.healthy = lambda: False
        async with self.client.get(self.base + "/_healthz/") as response:
            self.assertEqual(response.status, 503)


if __name__ == "__main__":
    unittest.main()

