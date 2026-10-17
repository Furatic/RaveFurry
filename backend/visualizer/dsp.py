"""Bounded stereo FFT analysis. No database, network, playback or file access."""
from __future__ import annotations

import math
import time
import numpy as np

BAND_COUNT = 48
FFT_SIZE = 2048
# UI order: blue bass, purple presence, orange body, green treble.
# Green deliberately starts in the audible presence range, not ultrasound.
CLOUD_RANGES = ((30, 180), (650, 2400), (180, 650), (2400, 12000))


class RhythmContext:
    """Eight seconds of scalar novelty, never audio or an unbounded event list.

    A causal, 50 Hz history measures local repetition four times per second.
    Its phase can support a real attack; it never emits a predicted beat.
    """
    RATE = 50
    LENGTH = 8 * RATE

    def __init__(self):
        self.history = np.zeros((self.LENGTH, 4), dtype=np.float32)
        self.bucket = np.zeros(4)
        self.position = self.count = 0
        self.next_tick = self.last_check = None
        self.period = self.confidence = self.anchor = 0.0
        self.regularity = np.zeros(4)
        self.thresholds = np.full(4, .045)

    def reset(self):
        self.history.fill(0)
        self.bucket.fill(0)
        self.position = self.count = 0
        self.next_tick = self.last_check = None
        self.period = self.confidence = self.anchor = 0.0
        self.regularity.fill(0)
        self.thresholds.fill(.045)

    def observe(self, novelty, now):
        if self.next_tick is None:
            self.next_tick = now
        if now - self.next_tick > .25 or now < self.next_tick - .25:
            self.reset()
            self.next_tick = now
        np.maximum(self.bucket, novelty, out=self.bucket)
        if now < self.next_tick:
            return
        # At most 13 ticks can be filled: a stalled capture never grows a queue.
        while now >= self.next_tick:
            self.history[self.position] = self.bucket
            self.position = (self.position + 1) % self.LENGTH
            self.count = min(self.LENGTH, self.count + 1)
            self.next_tick += 1 / self.RATE
            self.bucket.fill(0)
        if self.last_check is not None and now - self.last_check < .25:
            return
        self.last_check = now
        if self.count < 100:
            return
        indices = (np.arange(self.count) + self.position - self.count) % self.LENGTH
        recent = self.history[indices]
        # A robust four-second noise floor does not chase each syllable/hit.
        local = recent[-200:]
        median = np.median(local, axis=0)
        deviation = np.median(np.abs(local - median), axis=0)
        self.thresholds[:] = np.maximum(.045, median + 3 * deviation)
        # Weight the last phrase most strongly while retaining several beats.
        age = np.arange(self.count - 1, -1, -1) / self.RATE
        curves = np.maximum(0, recent - np.mean(recent, axis=0)) * np.exp(-age[:, None] / 4)
        combined = np.max(curves, axis=1)
        correlations = []
        for curve in (*curves.T, combined):
            corr = np.correlate(curve, curve, mode="full")[self.count - 1:]
            # Normalize overlap energy, not by sample count or silence duration.
            energy = np.cumsum(curve * curve)
            lags = np.arange(10, min(76, self.count // 2))
            denominator = np.sqrt(energy[self.count - lags - 1] * (energy[-1] - energy[lags - 1]))
            correlations.append(np.clip(corr[lags] / np.maximum(1e-9, denominator), 0, 1))
        self.regularity[:] = [float(np.max(c)) for c in correlations[:4]]
        # A beat rarely lands on exactly the same 20 ms bucket every time.
        # Integrate neighboring lag evidence so a long integer alias does not
        # win merely because a 180 BPM beat falls between 50 Hz samples.
        raw = correlations[4]
        scores = np.convolve(raw, [.25, .50, .25], mode="same")
        # Prefer the first supported repetition over its 2x/3x lag aliases.
        peaks = np.flatnonzero((scores[1:-1] >= scores[:-2]) & (scores[1:-1] > scores[2:])) + 1
        if not peaks.size:
            self.confidence *= .65
            return
        best = float(np.max(scores[peaks]))
        supported = peaks[scores[peaks] >= max(.20, best * .72)]
        if not supported.size or float(np.max(combined[-100:])) < .045:
            self.confidence *= .65
            return
        choice = int(supported[0])
        # Hysteresis avoids flipping between tempo octaves on every check.
        if self.period:
            old = int(np.argmin(np.abs(lags / self.RATE - self.period)))
            if scores[old] >= best * .90 and old <= int(supported[0]) + 2:
                choice = old
        proposed = float(lags[choice] / self.RATE)
        strength = float(scores[choice])
        self.confidence = float(np.clip((strength - .15) / .45, 0, 1))
        self.period = proposed
        width = min(self.count, round(proposed * self.RATE * 2))
        tail = combined[-width:]
        # Prefer the latest of comparably strong real attacks for the phase.
        candidates = np.flatnonzero(tail >= max(.04, float(tail.max()) * .75))
        if candidates.size:
            offset = width - 1 - int(candidates[-1])
            self.anchor = self.next_tick - 1 / self.RATE - offset / self.RATE

    def support(self, now):
        if self.period <= 0 or self.confidence < .35:
            return 0.0
        distance = abs((now - self.anchor + self.period / 2) % self.period - self.period / 2)
        width = max(.035, self.period * .12)
        main = math.exp(-.5 * (distance / width) ** 2)
        # Alternating kick/snare/hat rhythms have genuine half-period attacks.
        half = self.period / 2
        subdivision = abs((now - self.anchor + half / 2) % half - half / 2)
        return self.confidence * max(main, .78 * math.exp(-.5 * (subdivision / width) ** 2))


def empty_frame():
    return {
        "version": 1, "seq": 0, "level": 0.0, "bass": 0.0,
        "mid": 0.0, "treble": 0.0, "activity": 0.0,
        "bands": [0.0] * BAND_COUNT, "rms": [0.0, 0.0],
        "peak": [0.0, 0.0], "kickId": 0, "kick": 0.0,
        "kickAt": 0.0, "accentId": 0, "accent": 0.0,
        "accentAt": 0.0, "capturedAt": time.monotonic(),
        "cloudLevels": [0.0] * 4, "cloudBalance": [0.0] * 4,
        "cloudFlux": [0.0] * 4, "cloudIds": [0] * 4,
        "cloudAt": [0.0] * 4, "cloudStrength": [0.0] * 4,
        "rhythmConfidence": 0.0, "rhythmTempo": 0.0, "rhythmDrive": 0.0,
    }


class Analyzer:
    """One fixed rolling window; stereo power cannot cancel in antiphase."""

    def __init__(self, rate=48000, channels=2):
        self.rate = int(rate)
        self.channels = int(channels)
        if not 8000 <= self.rate <= 192000 or not 1 <= self.channels <= 8:
            raise ValueError("Unsupported analysis format")
        self.samples = np.zeros((FFT_SIZE, self.channels), dtype=np.float32)
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        self.scale = 2.0 / float(self.window.sum())
        self.hop = max(128, round(self.rate / 60))
        self.pending = 0
        self.collected = 0
        self.frequencies = np.fft.rfftfreq(FFT_SIZE, 1.0 / self.rate)
        edges = np.geomspace(30, min(18000, self.rate * .47), BAND_COUNT + 1)
        self.groups = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            indices = np.flatnonzero((self.frequencies >= lo) & (self.frequencies < hi))
            if not indices.size:
                indices = np.array([int(np.argmin(abs(self.frequencies - math.sqrt(lo * hi))))])
            self.groups.append(indices)
        self.regions = [
            (self.frequencies >= lo) & (self.frequencies < hi)
            for lo, hi in [(30, 220), (220, 3500), (3500, self.rate * .48)]
        ]
        # Log-spaced maxima plus a neighboring-frequency maximum reference
        # follow moving harmonics rather than mistaking vibrato for new notes.
        onset_edges = np.geomspace(30, min(12000, self.rate * .47), 97)
        self.onset_groups = []
        for lo, hi in zip(onset_edges[:-1], onset_edges[1:]):
            indices = np.flatnonzero((self.frequencies >= lo) & (self.frequencies < hi))
            if not indices.size:
                indices = np.array([int(np.argmin(abs(self.frequencies - math.sqrt(lo * hi))))])
            self.onset_groups.append(indices)
        self.onset_frequencies = np.sqrt(onset_edges[:-1] * onset_edges[1:])
        self.onset_previous = np.zeros((2, 96))
        self.onset_db_previous = np.full((2, 4), -180.0)
        self.previous_bins = np.zeros(FFT_SIZE // 2 + 1)
        self.bass_flux_mean = self.bass_flux_deviation = 0.0
        self.onset_position = 0
        self.rhythm = RhythmContext()
        self.pending_at = self.pending_peak_at = self.pending_score = 0.0
        self.pending_strength = np.zeros(4)
        self.pending_flux = np.zeros(4)
        self.silent_for = 0.0
        # Soft crossovers keep a vocal harmonic near a boundary from abruptly
        # jumping between colors. The four weights partition spectral power.
        cloud_weights = []
        edge_width = math.log(1.12)
        for lo, hi in CLOUD_RANGES:
            lower = np.clip((np.log(np.maximum(self.frequencies, 1) / lo) + edge_width) / (2 * edge_width), 0, 1)
            upper = np.clip((np.log(np.maximum(self.frequencies, 1) / hi) + edge_width) / (2 * edge_width), 0, 1)
            cloud_weights.append(lower * lower * (3 - 2 * lower) * (1 - upper * upper * (3 - 2 * upper)))
        self.cloud_weights = np.array(cloud_weights)
        weight_sum = self.cloud_weights.sum(axis=0)
        self.cloud_weights /= np.maximum(1, weight_sum)
        # Assign the analysis bins with the same soft color boundaries.
        self.onset_weights = np.array([
            np.interp(self.onset_frequencies, self.frequencies, weight)
            for weight in self.cloud_weights
        ])
        self.flux_average = np.zeros(4)
        self.flux_deviation = np.zeros(4)
        self.cloud_reference = np.full(4, -65.0)
        self.cloud_levels = np.zeros(4)
        self.smoothed_bands = np.zeros(BAND_COUNT)
        self.last = None
        self.activity = 0.0
        self.frame = empty_frame()

    @staticmethod
    def _level(amplitude, floor=-65.0, span=55.0):
        return np.clip((20 * np.log10(np.maximum(amplitude, 1e-9)) - floor) / span, 0, 1)

    def feed(self, data, timestamp=None):
        now = time.monotonic() if timestamp is None else float(timestamp)
        if not math.isfinite(now):
            return None
        chunk = np.frombuffer(data, dtype="<f4")
        count = chunk.size // self.channels
        if count == 0:
            return None
        chunk = chunk[:count * self.channels].reshape(count, self.channels)
        # Reject corrupt/unsupported input without ever forwarding PCM.
        chunk = np.clip(np.nan_to_num(chunk, nan=0.0, posinf=0.0, neginf=0.0), -4, 4)
        if count >= FFT_SIZE:
            self.samples[:] = chunk[-FFT_SIZE:]
        else:
            self.samples[:-count] = self.samples[count:]
            self.samples[-count:] = chunk
        self.pending += count
        self.collected = min(FFT_SIZE, self.collected + count)
        if self.pending < self.hop:
            return None
        self.pending %= self.hop
        elapsed = now - self.last if self.last is not None else 1 / 60
        if elapsed > .30 or elapsed <= 0:
            self.rhythm.reset()
            self.onset_previous.fill(0)
            self.onset_db_previous.fill(-180)
            self.previous_bins.fill(0)
            self.bass_flux_mean = self.bass_flux_deviation = 0.0
            self.flux_average.fill(0)
            self.flux_deviation.fill(0)
            self.pending_score = 0.0
        dt = min(.25, max(.001, elapsed))
        self.last = now

        transformed = np.fft.rfft(self.samples * self.window[:, None], axis=0)
        power = np.mean(np.abs(transformed * self.scale) ** 2, axis=1)
        amplitude = np.sqrt(power)
        rms = np.sqrt(np.mean(self.samples ** 2, axis=0))
        peak = np.max(np.abs(chunk), axis=0)
        overall = float(np.sqrt(np.mean(rms ** 2)))
        audible = overall > .00006  # approximately -84 dBFS
        raw_bands = np.array([np.sqrt(np.sum(power[g])) for g in self.groups])
        targets = self._level(raw_bands, -70, 60) if audible else np.zeros(BAND_COUNT)
        tau = np.where(targets > self.smoothed_bands, .018, .18)
        self.smoothed_bands += (targets - self.smoothed_bands) * (1 - np.exp(-dt / tau))
        region_values = [float(np.sqrt(np.sum(power[g]))) for g in self.regions]
        cloud_power = self.cloud_weights @ power
        cloud_amplitude = np.sqrt(cloud_power)
        balance = cloud_power / max(1e-12, float(cloud_power.sum())) if audible else np.zeros(4)
        absolute = self._level(cloud_amplitude, -68, 58) if audible else np.zeros(4)
        db = 20 * np.log10(np.maximum(cloud_amplitude, 1e-9))
        # Local contrast brings out changes in mastered/compressed music, without
        # making silence or a constant tone generate invented rhythmic pulses.
        contrast = np.clip((db - self.cloud_reference) / 12, -1, 1)
        level = float(self._level(overall, -65, 55)) if audible else 0.0
        presence = np.sqrt(np.clip(balance / .015, 0, 1))
        targets_cloud = np.clip((absolute * (.72 + .13 * contrast) + .28 * level * np.sqrt(balance)) * presence, 0, 1)
        tau_cloud = np.where(targets_cloud > self.cloud_levels, .035, .24)
        self.cloud_levels += (targets_cloud - self.cloud_levels) * (1 - np.exp(-dt / tau_cloud))
        self.cloud_reference += (db - self.cloud_reference) * (1 - math.exp(-dt / 2.0))

        # A SuperFlux-style frequency maximum suppresses pitch movement. The
        # two-frame reference preserves actual attacks in compressed mixtures.
        # Integrate a peak's neighboring Hann lobes first. Otherwise a tone
        # moving between FFT-bin centers changes its tallest bin even though
        # its energy is constant (especially low vocal fundamentals).
        peak_energy = np.sqrt(power + np.r_[0, power[:-1]] + np.r_[power[1:], 0])
        onset_amplitude = np.array([float(np.max(peak_energy[g])) for g in self.onset_groups])
        reference = self.onset_previous[self.onset_position]
        maximum = np.maximum.reduce((reference, np.r_[reference[0], reference[:-1]], np.r_[reference[1:], reference[-1]]))
        relative_flux = np.maximum(0, onset_amplitude - maximum) / (onset_amplitude + maximum + .0005)
        onset_power = onset_amplitude ** 2
        region_power = self.onset_weights @ onset_power
        flux = np.array([
            math.sqrt(float(np.sum(relative_flux ** 2 * onset_power * weight)) / max(1e-12, region_power[i]))
            for i, weight in enumerate(self.onset_weights)
        ])
        # Pitch/timbre motion alone should animate continuous color/size, not
        # create a strobe. A discrete pulse also needs a real regional energy
        # attack. A soft gate preserves quieter percussion above sustained beds.
        rise = np.maximum(0, db - self.onset_db_previous[self.onset_position])
        flux *= np.sqrt(np.clip((rise - .15) / 1.5, 0, 1))
        # Corroborate bass attacks in the original FFT bins as well. Closely
        # spaced sustained bass notes and vocal intermodulation can change a
        # broad log-bin maximum without supplying a convincing new drum hit.
        bin_change = np.maximum(0, amplitude - self.previous_bins) / (amplitude + self.previous_bins + .0005)
        bass_flux = math.sqrt(float(np.sum(bin_change ** 2 * power * self.cloud_weights[0])) / max(1e-12, cloud_power[0]))
        bass_threshold = max(.035, self.bass_flux_mean * 2.3 + self.bass_flux_deviation * .8)
        thresholds = np.maximum(.045, .65 * (self.flux_average * 2.0 + self.flux_deviation) + .35 * self.rhythm.thresholds)
        # A finite FFT smears tones into neighboring bins. Require actual local
        # spectral peaks in a region, rather than lighting bass from the tail of
        # a vibrating vocal just above its boundary. Quiet real instruments and
        # broadband percussion still qualify even in a dense, louder mix.
        spectral_peaks = np.zeros_like(power, dtype=bool)
        spectral_peaks[1:-1] = (power[1:-1] >= power[:-2]) & (power[1:-1] > power[2:])
        onset_support = (self.cloud_weights @ (power * spectral_peaks)) / np.maximum(1e-12, cloud_power)
        frame = dict(self.frame)
        ids, onset_at, strengths = [list(frame[name]) for name in ("cloudIds", "cloudAt", "cloudStrength")]
        eligible = (cloud_amplitude > .0007) & (balance > .003) & (onset_support > .10)
        if self.collected < FFT_SIZE or not audible:
            eligible[:] = False
        observed = np.where(eligible, flux, 0)
        self.rhythm.observe(observed, now)
        support = self.rhythm.support(now)
        # Recent periodicity helps weaker attacks on the established rhythm.
        # Off-grid changes still qualify; syncopation is not forced to a grid.
        adjusted = thresholds * (1.10 - .30 * support * np.maximum(.5, self.rhythm.regularity))
        # With a stable rhythm, marginal off-pattern fluctuations need stronger
        # evidence. Strong syncopated attacks still qualify; no event is moved
        # onto a grid or generated without a new measured attack.
        temporal_weight = 1 - .60 * max(0, self.rhythm.confidence - support)
        scores = np.where(eligible, flux / adjusted * temporal_weight, 0)
        scores[0] = min(scores[0], bass_flux / bass_threshold * (1 + .25 * support))
        score = float(np.max(scores))
        # Confirm one coherent attack episode after a falling peak (one frame)
        # or 40 ms, instead of independently retriggering four colors while
        # different FFT bins fluctuate. The peak retains its capture timestamp.
        # Refractory timing is regional: a preceding vocal/hat must not lock
        # out a kick in another cloud. Each cloud still has its own rate limit.
        if score > 1:
            if self.pending_score == 0:
                self.pending_at = now
                self.pending_flux.fill(0)
                self.pending_strength.fill(0)
            if score > self.pending_score:
                self.pending_score, self.pending_peak_at = score, now
            novelty = np.clip(flux / np.maximum(.08, adjusted * 2), 0, 1)
            # A soft syllable can change spectral color without receiving
            # the full flash of a sharp drum attack. Continuous energy is
            # still rendered independently, including sustained singing.
            sharpness = np.sqrt(np.clip(rise / 6, 0, 1))
            improved = scores > self.pending_flux
            self.pending_strength[improved] = (absolute * (.45 + .55 * novelty) * presence * (.30 + .70 * sharpness))[improved]
            np.maximum(self.pending_flux, scores, out=self.pending_flux)
        accents = []
        if self.pending_score and (score < self.pending_score * .82 or now - self.pending_at >= .040):
            strongest = float(np.max(self.pending_flux))
            accepted_strength = 0.0
            for i in range(4):
                # Minor harmonic spill receives less light; simultaneous real
                # attacks share the same timestamp rather than changing owner.
                if self.pending_flux[i] < 1.0:
                    continue
                minimum_gap = .14 if i == 0 else .16
                if self.pending_peak_at - onset_at[i] < minimum_gap:
                    continue
                strength = float(self.pending_strength[i]) * math.sqrt(min(1, self.pending_flux[i] / strongest))
                ids[i] += 1
                onset_at[i], strengths[i] = self.pending_peak_at, round(strength, 4)
                accepted_strength = max(accepted_strength, strength)
                if i == 0:
                    frame.update(kickId=frame["kickId"] + 1, kick=round(strength, 4), kickAt=onset_at[i])
                else:
                    accents.append(strength)
            self.activity = min(1, self.activity + .15 * accepted_strength)
            self.pending_score = 0.0
        self.silent_for = 0.0 if audible else self.silent_for + dt
        if self.silent_for > .40:
            self.rhythm.reset()
            self.pending_score = 0.0
        # Retain the version-1 fields for older local HTML clients.
        if accents:
            frame.update(accentId=frame["accentId"] + 1, accent=round(max(accents), 4), accentAt=self.pending_peak_at)
        adaptation = 1 - math.exp(-dt / .50)
        self.flux_deviation += (np.abs(flux - self.flux_average) - self.flux_deviation) * adaptation
        self.flux_average += (flux - self.flux_average) * adaptation
        self.bass_flux_deviation += (abs(bass_flux - self.bass_flux_mean) - self.bass_flux_deviation) * adaptation
        self.bass_flux_mean += (bass_flux - self.bass_flux_mean) * adaptation
        self.previous_bins[:] = amplitude
        self.onset_previous[self.onset_position] = onset_amplitude
        self.onset_db_previous[self.onset_position] = db
        self.onset_position = (self.onset_position + 1) % 2
        self.activity *= math.exp(-dt / 1.8)

        stereo_rms = (list(rms[:2]) if self.channels >= 2 else [rms[0], rms[0]])
        stereo_peak = (list(peak[:2]) if self.channels >= 2 else [peak[0], peak[0]])
        frame.update({
            "seq": frame["seq"] + 1, "capturedAt": now,
            "level": round(float(self._level(overall, -65, 55)) if audible else 0.0, 4),
            "bass": round(float(self._level(region_values[0], -65, 55)) if audible else 0.0, 4),
            "mid": round(float(self._level(region_values[1], -65, 55)) if audible else 0.0, 4),
            "treble": round(float(self._level(region_values[2], -70, 60)) if audible else 0.0, 4),
            "activity": round(self.activity, 4),
            "bands": np.round(self.smoothed_bands, 4).tolist(),
            "rms": [round(float(v), 6) for v in stereo_rms],
            "peak": [round(float(v), 6) for v in stereo_peak],
            "cloudLevels": np.round(self.cloud_levels, 4).tolist(),
            "cloudBalance": np.round(balance, 4).tolist(),
            "cloudFlux": np.round(np.clip(flux, 0, 1), 4).tolist(),
            "cloudIds": ids, "cloudAt": onset_at, "cloudStrength": strengths,
            "rhythmConfidence": round(self.rhythm.confidence, 4),
            "rhythmTempo": round(60 / self.rhythm.period, 2) if self.rhythm.confidence > .35 and self.rhythm.period else 0.0,
            "rhythmDrive": round(float(np.clip(.55 * self.activity + .45 * self.rhythm.confidence * np.clip((60 / self.rhythm.period - 70) / 110, 0, 1), 0, 1)), 4) if audible and self.rhythm.period else round(.55 * self.activity, 4) if audible else 0.0,
        })
        self.frame = frame
        return frame

