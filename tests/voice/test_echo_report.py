"""Echo with signal report (private call to 9999): recording, quality numbers, spoken words."""

import pytest

from adn_server.application.echo_report import (
    EchoReportSession,
    clip_texts,
    decimal_words,
    language_for,
    number_words,
    report_words,
)

VOICE = bytes(range(33))


def _session(**kw):
    return EchoReportSession(stream_id=b"\x00\x00\x00\x01", rf_src=2130035, peer_id=b"\x0c\xb2\x6f\x0f", **kw)


def test_language_from_country_prefix() -> None:
    assert language_for(2130035) == "es"   # Andorra
    assert language_for(2142346) == "es"   # Spain
    assert language_for(213003515) == "es" # hotspot ID of an Andorran
    assert language_for(3101234) == "en"   # USA
    assert language_for(2226289) == "en"   # Italy: English until its clips exist
    assert language_for(12345) == "en"


def test_recording_keeps_the_ambe_bits_like_the_recorder() -> None:
    s = _session()
    s.add_packet(0, VOICE, True, 0, 0)
    bits = int.from_bytes(VOICE, "big")
    want = ((bits >> 156) << 108) | (bits & ((1 << 108) - 1))
    assert s.recording() == want.to_bytes(27, "big")


def test_ber_rssi_and_loss() -> None:
    s = _session()
    for seq in (0, 1, 2, 5, 6):  # 3 and 4 lost
        s.add_packet(seq, VOICE, True, 3, 62)
    assert s.lost == 2 and s.packets == 5
    assert s.loss_percent == pytest.approx(100 * 2 / 7)
    assert s.ber_percent == pytest.approx(100 * 15 / (141 * 5))
    assert s.rssi_dbm == -62


def test_sequence_wraps_without_counting_loss() -> None:
    s = _session()
    for seq in (254, 255, 0, 1):
        s.add_packet(seq, VOICE, True, 0, 50)
    assert s.lost == 0


def test_no_rf_data_from_network_clients() -> None:
    s = _session()
    for seq in range(4):
        s.add_packet(seq, VOICE, True, 0, 0)
    assert s.ber_percent is None and s.rssi_dbm is None
    assert report_words(s, "en")[:2] == ["er_nosignaldata", "er_loss"]


@pytest.mark.parametrize("n,lang,words", [
    (0, "es", ["er_n0"]), (21, "es", ["er_n21"]), (62, "es", ["er_n60", "er_and", "er_n2"]),
    (100, "es", ["er_n100"]), (120, "es", ["er_hundred", "er_n20"]), (135, "es", ["er_hundred", "er_n30", "er_and", "er_n5"]),
    (17, "en", ["er_n17"]), (62, "en", ["er_n60", "er_n2"]), (120, "en", ["er_hundred", "er_and", "er_n20"]),
])
def test_numbers(n, lang, words) -> None:
    assert number_words(n, lang) == words


def test_decimals() -> None:
    assert decimal_words(0.4, "es") == ["er_n0", "er_point", "er_n4"]
    assert decimal_words(3.0, "en") == ["er_n3"]


def test_full_report_and_every_word_has_a_clip() -> None:
    s = _session()
    for seq in range(10):
        s.add_packet(seq, VOICE, True, 1, 47)
    for lang in ("es", "en"):
        words = report_words(s, lang)
        assert words[0] == "er_ber" and "er_signal" in words and "er_loss" in words
        missing = set(words) - set(clip_texts(lang))
        assert not missing, (lang, missing)
    for lang in ("es", "en"):  # every number the report can say has its clip
        texts = clip_texts(lang)
        for n in range(200):
            assert set(number_words(n, lang)) <= set(texts), (lang, n)


# ---- playback: echo, silence, report, through the server voice path ---------------------------

from unittest.mock import patch  # noqa: E402

from tests.harness.voice_helpers import FakeVoiceProvider, voice_master_scenario  # noqa: E402

from adn_server.application.echo_report import clip_texts as _clips  # noqa: E402
from adn_server.application.voice_use_cases import VoiceUseCases  # noqa: E402


class _Provider(FakeVoiceProvider):
    def __init__(self, with_clips: bool) -> None:
        self.with_clips = with_clips
        self.phrases: list = []

    def get_ambe_words(self, languages, audio_path):
        words = {"silence": ["S"]}
        if self.with_clips:
            words.update({k: [k] for k in _clips("es")})
        return {languages: words}

    def pairs_from_bytes(self, data):
        return ["REC"] * (len(data) // 27)

    def pkt_gen(self, rf_src, dst_id, peer, slot, phrase):
        self.phrases.append((rf_src, dst_id, phrase))
        return super().pkt_gen(rf_src, dst_id, peer, slot, phrase)


def _play(with_clips: bool):
    scenario, master = voice_master_scenario()
    provider = _Provider(with_clips)
    uc = VoiceUseCases(provider, scenario.config, get_protocols=lambda: {"MASTER-A": master},
                       call_from_reactor=lambda fn, *a: fn(*a), audio_path="/audio")
    s = _session()
    for seq in range(4):
        s.add_packet(seq, VOICE, True, 2, 55)
    with patch("adn_server.application.voice_use_cases.time.sleep"), \
         patch.object(VoiceUseCases, "play_on_slot", return_value=3) as play:
        uc.play_echo_report("MASTER-A", s)
    return provider, play


def test_playback_is_echo_then_report_on_tg9_from_the_server_id() -> None:
    provider, play = _play(with_clips=True)
    (rf_src, dst_id, phrase), = provider.phrases
    assert dst_id == (9).to_bytes(3, "big") and rf_src == (1000001).to_bytes(3, "big")
    assert phrase[0] == ["REC"] * 4 and phrase[1] == ["S"] and phrase[2] == ["S"]
    assert [w[0] for w in phrase[3:]] == report_words(_session_with(2, 55), "es")
    play.assert_called_once()


def test_report_event_for_plugins() -> None:
    scenario, master = voice_master_scenario()
    uc = VoiceUseCases(_Provider(True), scenario.config, get_protocols=lambda: {"MASTER-A": master},
                       call_from_reactor=lambda fn, *a: fn(*a), audio_path="/audio")
    events = []
    uc.set_echo_report_listener(events.append)
    s = _session_with(2, 55)
    with patch("adn_server.application.voice_use_cases.time.sleep"), \
         patch.object(VoiceUseCases, "play_on_slot", return_value=3):
        uc.play_echo_report("MASTER-A", s)
    (ev,) = events
    assert (ev.src_id, ev.rssi_dbm, ev.language, ev.packets) == (2130035, -55, "es", 4)
    assert ev.ber_percent == s.ber_percent


def test_missing_clips_play_the_echo_alone() -> None:
    provider, play = _play(with_clips=False)
    (_, _, phrase), = provider.phrases
    assert len(phrase) == 3 and phrase[0] == ["REC"] * 4
    play.assert_called_once()


def _session_with(ber, rssi):
    s = _session()
    for seq in range(4):
        s.add_packet(seq, VOICE, True, ber, rssi)
    return s
