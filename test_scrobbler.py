"""Tests for the vinyl scrobbler."""

import os
import time
from unittest.mock import MagicMock, patch, Mock

import pytest
import requests

from scrobbler import (
    ScrobbleState,
    rms_of_raw_24bit,
    submit_to_listenbrainz,
    recognize_track,
    attempt_recognition,
    cleanup_temp_file,
    load_config,
    load_blocklist,
    is_blocked,
)


# ---------------------------------------------------------------------------
# rms_of_raw_24bit
# ---------------------------------------------------------------------------


class TestRmsOfRaw24Bit:
    def test_empty_input(self):
        assert rms_of_raw_24bit(b"") == 0.0

    def test_too_short(self):
        assert rms_of_raw_24bit(b"\x00\x00") == 0.0

    def test_silence(self):
        # 24-bit silence: all zeros, 10 samples (30 bytes)
        data = b"\x00\x00\x00" * 10
        assert rms_of_raw_24bit(data, stride=1) == 0.0

    def test_known_amplitude(self):
        # Single 24-bit sample with value 1000
        val = (1000).to_bytes(3, "little", signed=True)
        assert rms_of_raw_24bit(val, stride=1) == 1000.0

    def test_negative_values(self):
        # Single negative sample: -1000
        val = (-1000).to_bytes(3, "little", signed=True)
        # RMS squares the value, so sign doesn't matter
        assert rms_of_raw_24bit(val, stride=1) == 1000.0

    def test_mixed_values(self):
        # Two samples: +1000 and -1000 — RMS should be 1000
        pos = (1000).to_bytes(3, "little", signed=True)
        neg = (-1000).to_bytes(3, "little", signed=True)
        data = pos + neg
        assert rms_of_raw_24bit(data, stride=1) == 1000.0

    def test_max_amplitude(self):
        # Max positive 24-bit value: 2^23 - 1 = 8388607
        max_val = (8388607).to_bytes(3, "little", signed=True)
        assert rms_of_raw_24bit(max_val, stride=1) == 8388607.0

    def test_stride_skips_samples(self):
        # 4 samples, stride=2 should only check samples 0 and 2
        s0 = (100).to_bytes(3, "little", signed=True)
        s1 = (9999).to_bytes(3, "little", signed=True)  # skipped
        s2 = (100).to_bytes(3, "little", signed=True)
        s3 = (9999).to_bytes(3, "little", signed=True)  # skipped
        data = s0 + s1 + s2 + s3
        assert rms_of_raw_24bit(data, stride=2) == 100.0

    def test_stride_default(self):
        # Default stride is 16 — with 16 samples, should check only the first
        sample = (500).to_bytes(3, "little", signed=True)
        data = sample * 16
        result = rms_of_raw_24bit(data)
        assert result == 500.0


# ---------------------------------------------------------------------------
# ScrobbleState
# ---------------------------------------------------------------------------


class TestScrobbleState:
    def test_initial_state(self):
        state = ScrobbleState()
        assert state.artist is None
        assert state.track is None

    def test_is_duplicate_false_when_empty(self):
        state = ScrobbleState()
        assert not state.is_duplicate("Artist", "Track")

    def test_is_duplicate_true_after_scrobble(self):
        state = ScrobbleState()
        state.record_scrobble("Artist", "Track")
        assert state.is_duplicate("Artist", "Track")

    def test_is_duplicate_false_different_track(self):
        state = ScrobbleState()
        state.record_scrobble("Artist", "Track A")
        assert not state.is_duplicate("Artist", "Track B")

    def test_is_duplicate_false_different_artist(self):
        state = ScrobbleState()
        state.record_scrobble("Artist A", "Track")
        assert not state.is_duplicate("Artist B", "Track")

    def test_record_scrobble_updates_state(self):
        state = ScrobbleState()
        before = time.time()
        state.record_scrobble("Artist", "Track")
        after = time.time()

        assert state.artist == "Artist"
        assert state.track == "Track"
        assert before <= state.scrobbled_at <= after


# ---------------------------------------------------------------------------
# submit_to_listenbrainz
# ---------------------------------------------------------------------------


class TestSubmitToListenbrainz:
    def test_successful_scrobble(self):
        state = ScrobbleState()
        mock_resp = Mock(status_code=200)

        with patch("scrobbler.requests.post", return_value=mock_resp) as mock_post:
            result = submit_to_listenbrainz(
                {"artist": "Songs: Ohia", "track": "The Old Black Hen"},
                token="test-token",
                state=state,
            )

        assert result is True
        assert state.artist == "Songs: Ohia"
        assert state.track == "The Old Black Hen"

        # Verify the API call
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["headers"]["Authorization"] == "Token test-token"
        payload = call_kwargs[1]["json"]
        assert payload["listen_type"] == "single"
        meta = payload["payload"][0]["track_metadata"]
        assert meta["artist_name"] == "Songs: Ohia"
        assert meta["track_name"] == "The Old Black Hen"
        assert meta["additional_info"]["submission_client"] == "vinyl-scrobbler"

    def test_skips_duplicate(self):
        state = ScrobbleState()
        state.record_scrobble("Artist", "Track")

        with patch("scrobbler.requests.post") as mock_post:
            result = submit_to_listenbrainz(
                {"artist": "Artist", "track": "Track"},
                token="test-token",
                state=state,
            )

        assert result is False
        mock_post.assert_not_called()

    def test_api_error_returns_false(self):
        state = ScrobbleState()
        mock_resp = Mock(status_code=401, text="Invalid token")

        with patch("scrobbler.requests.post", return_value=mock_resp):
            result = submit_to_listenbrainz(
                {"artist": "Artist", "track": "Track"},
                token="bad-token",
                state=state,
            )

        assert result is False
        assert state.artist is None  # Not recorded

    def test_network_error_retries_three_times(self):
        state = ScrobbleState()

        with patch("scrobbler.requests.post", side_effect=requests.ConnectionError("timeout")):
            with patch("scrobbler.time.sleep"):  # Don't actually sleep
                result = submit_to_listenbrainz(
                    {"artist": "Artist", "track": "Track"},
                    token="test-token",
                    state=state,
                )

        assert result is False

    def test_network_error_succeeds_on_retry(self):
        state = ScrobbleState()
        mock_resp = Mock(status_code=200)

        with patch(
            "scrobbler.requests.post",
            side_effect=[requests.ConnectionError("fail"), mock_resp],
        ):
            with patch("scrobbler.time.sleep"):
                result = submit_to_listenbrainz(
                    {"artist": "Artist", "track": "Track"},
                    token="test-token",
                    state=state,
                )

        assert result is True
        assert state.artist == "Artist"

    def test_blocked_track_rejected(self):
        state = ScrobbleState()
        blocklist = {("tim maia", "ela partiu")}

        with patch("scrobbler.requests.post") as mock_post:
            result = submit_to_listenbrainz(
                {"artist": "Tim Maia", "track": "Ela Partiu"},
                token="test-token",
                state=state,
                blocklist=blocklist,
            )

        assert result is False
        mock_post.assert_not_called()
        assert state.artist is None  # Not recorded

    def test_blocklist_checked_before_duplicate(self):
        """Blocklist takes priority over duplicate check."""
        state = ScrobbleState()
        state.record_scrobble("Tim Maia", "Ela Partiu")
        blocklist = {("tim maia", "ela partiu")}

        with patch("scrobbler.requests.post") as mock_post:
            result = submit_to_listenbrainz(
                {"artist": "Tim Maia", "track": "Ela Partiu"},
                token="test-token",
                state=state,
                blocklist=blocklist,
            )

        assert result is False
        mock_post.assert_not_called()

    def test_empty_blocklist_allows_scrobble(self):
        state = ScrobbleState()
        mock_resp = Mock(status_code=200)

        with patch("scrobbler.requests.post", return_value=mock_resp):
            result = submit_to_listenbrainz(
                {"artist": "Mineral", "track": "LoveLetterTypewriter"},
                token="test-token",
                state=state,
                blocklist=set(),
            )

        assert result is True

    def test_none_blocklist_allows_scrobble(self):
        state = ScrobbleState()
        mock_resp = Mock(status_code=200)

        with patch("scrobbler.requests.post", return_value=mock_resp):
            result = submit_to_listenbrainz(
                {"artist": "Mineral", "track": "LoveLetterTypewriter"},
                token="test-token",
                state=state,
                blocklist=None,
            )

        assert result is True


# ---------------------------------------------------------------------------
# load_blocklist / is_blocked
# ---------------------------------------------------------------------------


class TestBlocklist:
    def test_load_from_file(self, tmp_path):
        f = tmp_path / "blocklist.txt"
        f.write_text("Tim Maia\tEla Partiu\nCitySound\tThe Open Road\n")
        result = load_blocklist(str(f))
        assert len(result) == 2
        assert ("tim maia", "ela partiu") in result
        assert ("citysound", "the open road") in result

    def test_load_ignores_comments_and_blanks(self, tmp_path):
        f = tmp_path / "blocklist.txt"
        f.write_text("# This is a comment\n\nTim Maia\tEla Partiu\n\n# Another\n")
        result = load_blocklist(str(f))
        assert len(result) == 1

    def test_load_ignores_malformed_lines(self, tmp_path):
        f = tmp_path / "blocklist.txt"
        f.write_text("No tab here\nTim Maia\tEla Partiu\n")
        result = load_blocklist(str(f))
        assert len(result) == 1
        assert ("tim maia", "ela partiu") in result

    def test_load_missing_file(self):
        result = load_blocklist("/nonexistent/blocklist.txt")
        assert result == set()

    def test_is_blocked_case_insensitive(self):
        blocklist = {("tim maia", "ela partiu")}
        assert is_blocked("TIM MAIA", "ELA PARTIU", blocklist)
        assert is_blocked("Tim Maia", "Ela Partiu", blocklist)
        assert is_blocked("tim maia", "ela partiu", blocklist)

    def test_is_blocked_false_for_unlisted(self):
        blocklist = {("tim maia", "ela partiu")}
        assert not is_blocked("Mineral", "LoveLetterTypewriter", blocklist)

    def test_is_blocked_empty_blocklist(self):
        assert not is_blocked("Anything", "Goes", set())


# ---------------------------------------------------------------------------
# recognize_track
# ---------------------------------------------------------------------------


class TestRecognizeTrack:
    def _make_mock_sig_gen(self, signatures):
        """Create a mock SignatureGenerator that yields given signatures."""
        mock_cls = MagicMock()
        instance = MagicMock()
        instance.get_next_signature = MagicMock(side_effect=signatures)
        instance.samples_processed = 0
        mock_cls.return_value = instance
        return mock_cls

    def test_match_found(self, tmp_path):
        sig_gen_cls = self._make_mock_sig_gen(["sig1", None])
        mock_recognize = MagicMock(return_value={
            "matches": [{"id": "123"}],
            "track": {
                "title": "Farewell Transmission",
                "subtitle": "Songs: Ohia",
            },
        })

        # Create a minimal valid audio file
        audio_file = tmp_path / "test.wav"
        self._write_test_wav(audio_file)

        result = recognize_track(
            str(audio_file),
            SignatureGenerator=sig_gen_cls,
            recognize_song_from_signature=mock_recognize,
        )

        assert result == {"artist": "Songs: Ohia", "track": "Farewell Transmission"}

    def test_no_match(self, tmp_path):
        sig_gen_cls = self._make_mock_sig_gen([None])
        mock_recognize = MagicMock()

        audio_file = tmp_path / "test.wav"
        self._write_test_wav(audio_file)

        result = recognize_track(
            str(audio_file),
            SignatureGenerator=sig_gen_cls,
            recognize_song_from_signature=mock_recognize,
        )

        assert result is None
        mock_recognize.assert_not_called()

    def test_match_with_missing_fields(self, tmp_path):
        sig_gen_cls = self._make_mock_sig_gen(["sig1", None])
        mock_recognize = MagicMock(return_value={
            "matches": [{"id": "123"}],
            "track": {},  # Missing title and subtitle
        })

        audio_file = tmp_path / "test.wav"
        self._write_test_wav(audio_file)

        result = recognize_track(
            str(audio_file),
            SignatureGenerator=sig_gen_cls,
            recognize_song_from_signature=mock_recognize,
        )

        assert result == {"artist": "Unknown Artist", "track": "Unknown Track"}

    def test_match_on_second_chunk(self, tmp_path):
        sig_gen_cls = self._make_mock_sig_gen(["sig1", "sig2", None])
        mock_recognize = MagicMock(side_effect=[
            {"matches": []},  # First chunk: no match
            {
                "matches": [{"id": "456"}],
                "track": {"title": "Ring the Bell", "subtitle": "Songs: Ohia"},
            },
        ])

        audio_file = tmp_path / "test.wav"
        self._write_test_wav(audio_file)

        result = recognize_track(
            str(audio_file),
            SignatureGenerator=sig_gen_cls,
            recognize_song_from_signature=mock_recognize,
        )

        assert result == {"artist": "Songs: Ohia", "track": "Ring the Bell"}
        assert mock_recognize.call_count == 2

    def test_recognition_exception_returns_none(self, tmp_path):
        sig_gen_cls = MagicMock(side_effect=Exception("SongRec crashed"))

        audio_file = tmp_path / "test.wav"
        self._write_test_wav(audio_file)

        result = recognize_track(
            str(audio_file),
            SignatureGenerator=sig_gen_cls,
            recognize_song_from_signature=MagicMock(),
        )

        assert result is None

    def _write_test_wav(self, path):
        """Write a minimal valid WAV file for pydub to parse."""
        from pydub import AudioSegment
        silence = AudioSegment.silent(duration=1000)  # 1s of silence
        silence.export(str(path), format="wav")


# ---------------------------------------------------------------------------
# attempt_recognition
# ---------------------------------------------------------------------------


class TestAttemptRecognition:
    def test_match_on_first_try(self, tmp_path):
        tmp_file = str(tmp_path / "capture.wav")
        record_fn = MagicMock(return_value=True)
        recognize_fn = MagicMock(return_value={"artist": "A", "track": "T"})

        result = attempt_recognition(
            tmp_file,
            record_durations=[20, 40],
            record_fn=record_fn,
            recognize_fn=recognize_fn,
        )

        assert result == {"artist": "A", "track": "T"}
        record_fn.assert_called_once_with(20, tmp_file)
        recognize_fn.assert_called_once_with(tmp_file)

    def test_match_on_second_try(self, tmp_path):
        tmp_file = str(tmp_path / "capture.wav")
        record_fn = MagicMock(return_value=True)
        recognize_fn = MagicMock(side_effect=[None, {"artist": "A", "track": "T"}])

        result = attempt_recognition(
            tmp_file,
            record_durations=[20, 40],
            record_fn=record_fn,
            recognize_fn=recognize_fn,
        )

        assert result == {"artist": "A", "track": "T"}
        assert record_fn.call_count == 2
        assert record_fn.call_args_list[0][0][0] == 20
        assert record_fn.call_args_list[1][0][0] == 40

    def test_no_match_after_all_tiers(self, tmp_path):
        tmp_file = str(tmp_path / "capture.wav")
        record_fn = MagicMock(return_value=True)
        recognize_fn = MagicMock(return_value=None)

        result = attempt_recognition(
            tmp_file,
            record_durations=[20, 40],
            record_fn=record_fn,
            recognize_fn=recognize_fn,
        )

        assert result is None
        assert record_fn.call_count == 2
        assert recognize_fn.call_count == 2

    def test_recording_failure_skips_to_next_tier(self, tmp_path):
        tmp_file = str(tmp_path / "capture.wav")
        record_fn = MagicMock(side_effect=[False, True])
        recognize_fn = MagicMock(return_value={"artist": "A", "track": "T"})

        with patch("scrobbler.time.sleep"):
            result = attempt_recognition(
                tmp_file,
                record_durations=[20, 40],
                record_fn=record_fn,
                recognize_fn=recognize_fn,
            )

        assert result == {"artist": "A", "track": "T"}
        recognize_fn.assert_called_once()

    def test_all_recordings_fail(self, tmp_path):
        tmp_file = str(tmp_path / "capture.wav")
        record_fn = MagicMock(return_value=False)
        recognize_fn = MagicMock()

        with patch("scrobbler.time.sleep"):
            result = attempt_recognition(
                tmp_file,
                record_durations=[20, 40],
                record_fn=record_fn,
                recognize_fn=recognize_fn,
            )

        assert result is None
        recognize_fn.assert_not_called()


# ---------------------------------------------------------------------------
# cleanup_temp_file
# ---------------------------------------------------------------------------


class TestCleanupTempFile:
    def test_removes_existing_file(self, tmp_path):
        f = tmp_path / "test.wav"
        f.write_text("data")
        cleanup_temp_file(str(f))
        assert not f.exists()

    def test_no_error_on_missing_file(self):
        cleanup_temp_file("/nonexistent/file.wav")  # Should not raise


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_missing_token_exits(self):
        env = {"ALSA_DEVICE": "hw:0,0"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(SystemExit):
                load_config()

    def test_defaults(self):
        env = {"LISTENBRAINZ_TOKEN": "test-token"}
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()

        assert cfg["token"] == "test-token"
        assert cfg["alsa_device"] == "hw:0,0"
        assert cfg["sample_rate"] == 48000
        assert cfg["channels"] == 2
        assert cfg["sample_format"] == "S24_3LE"
        assert cfg["silence_threshold"] == 500
        assert cfg["sustained_audio_checks"] == 3
        assert cfg["rms_stride"] == 16
        assert cfg["recognize_durations"] == [20, 40]
        assert cfg["recognition_cooldown"] == 10
        assert cfg["log_level"] == "INFO"

    def test_custom_env_vars(self):
        env = {
            "LISTENBRAINZ_TOKEN": "my-token",
            "ALSA_DEVICE": "hw:1,0",
            "SAMPLE_RATE": "44100",
            "CHANNELS": "1",
            "SAMPLE_FORMAT": "S16_LE",
            "SILENCE_THRESHOLD": "200",
            "SUSTAINED_AUDIO_CHECKS": "5",
            "RMS_STRIDE": "8",
            "RECOGNITION_COOLDOWN": "20",
            "LOG_LEVEL": "DEBUG",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()

        assert cfg["token"] == "my-token"
        assert cfg["alsa_device"] == "hw:1,0"
        assert cfg["sample_rate"] == 44100
        assert cfg["channels"] == 1
        assert cfg["sample_format"] == "S16_LE"
        assert cfg["silence_threshold"] == 200
        assert cfg["sustained_audio_checks"] == 5
        assert cfg["rms_stride"] == 8
        assert cfg["recognition_cooldown"] == 20
        assert cfg["log_level"] == "DEBUG"
