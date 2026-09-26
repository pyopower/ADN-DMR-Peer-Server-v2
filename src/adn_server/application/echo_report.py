# ADN DMR Peer Server - echo with signal report (private call to TG 9999)
#
###############################################################################
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software Foundation,
#   Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
###############################################################################

"""Echo with a spoken signal report: a private call to ``ECHO_REPORT_ID`` (9999).

The caller's transmission is recorded like the parrot does, and when it ends the server plays
it back followed by a short report of what reached the master:

- **BER**: bit errors the hotspot measured on the caller's RF (HBP DMRD byte 53, errors of the
  141 checked bits of each voice burst, as MMDVMHost reports them);
- **signal**: RSSI at the hotspot (byte 54, the magnitude of the dBm value);
- **loss**: voice packets missing on the way to the master, from the gaps in the DMRD sequence.

Hotspots and apps that don't measure RF (DVSwitch, network clients...) send 0 in both bytes:
then the report says there is no signal data and only gives the loss.

Pure logic, no I/O: :class:`EchoReportSession` collects one call, :func:`report_words` turns its
numbers into the word keys of the ``er_*`` AMBE clips (``Audio/<lang>/er_*.ambe``), in Spanish or
English chosen from the caller's country (:func:`language_for`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

ECHO_REPORT_ID = 9999
BITS_CHECKED_PER_BURST = 141  # MMDVMHost: errors are counted over these bits of each voice burst
MAX_RECORDED_BURSTS = 60 * 17  # one minute of voice, like a long QSO over

# Country prefixes (first three digits of a 7-digit DMR ID) where the report is spoken in Spanish;
# everyone else gets English until more languages have their clips.
SPANISH_PREFIXES = frozenset({
    213,  # Andorra (most of the network speaks Spanish; Catalan when its clips exist)
    214,  # Spain
    334,  # Mexico
    330,  # Puerto Rico
    368,  # Cuba
    370,  # Dominican Republic
    704, 706, 708, 710, 712, 714,  # Guatemala, El Salvador, Honduras, Nicaragua, Costa Rica, Panama
    716, 722, 730, 732, 734, 736, 740, 744, 748,  # Peru, Argentina, Chile, Colombia, Venezuela, Bolivia, Ecuador, Paraguay, Uruguay
})
LANG_DIRS = {"es": "es_ES", "en": "en_GB"}


def language_for(radio_id: int) -> str:
    """"es" or "en" from the country prefix of a DMR ID (7 digits, or 9 for a hotspot ID)."""
    text = str(radio_id)
    if len(text) in (7, 9) and int(text[:3]) in SPANISH_PREFIXES:
        return "es"
    return "en"


@dataclass
class EchoReportSession:
    """One call to the echo report: recorded bursts plus the per-packet quality data."""

    stream_id: bytes
    rf_src: int
    peer_id: bytes
    bursts: list[bytes] = field(default_factory=list)  # 27 bytes each: the two 108-bit AMBE halves
    bit_errors: int = 0
    rssi_sum: int = 0
    rssi_count: int = 0
    measured_bursts: int = 0
    packets: int = 0
    lost: int = 0
    _last_seq: int | None = None

    def add_packet(self, seq: int, dmrpkt: bytes, is_voice: bool, ber: int, rssi: int) -> None:
        """One DMRD of the call: ``dmrpkt`` its 33 payload bytes, ``ber``/``rssi`` DMRD bytes 53/54."""
        if self._last_seq is not None:
            gap = (seq - self._last_seq) % 256
            if 1 < gap < 128:  # a jump forward: packets lost; 0 or a big jump is a duplicate/reorder
                self.lost += gap - 1
        self._last_seq = seq
        self.packets += 1
        if not is_voice or len(dmrpkt) < 33:
            return
        if len(self.bursts) < MAX_RECORDED_BURSTS:
            bits = int.from_bytes(dmrpkt[:33], "big")
            # AMBE payload of a voice burst: bits 0-107 and 156-263 of the 264 (sync/EMB in between)
            first = bits >> (264 - 108)
            second = bits & ((1 << 108) - 1)
            self.bursts.append(((first << 108) | second).to_bytes(27, "big"))
        if ber or rssi:
            self.measured_bursts += 1
            self.bit_errors += ber
            if rssi:
                self.rssi_sum += rssi
                self.rssi_count += 1

    @property
    def ber_percent(self) -> float | None:
        if not self.measured_bursts:
            return None
        return 100.0 * self.bit_errors / (BITS_CHECKED_PER_BURST * self.measured_bursts)

    @property
    def rssi_dbm(self) -> int | None:
        return -round(self.rssi_sum / self.rssi_count) if self.rssi_count else None

    @property
    def loss_percent(self) -> float:
        total = self.packets + self.lost
        return 100.0 * self.lost / total if total else 0.0

    def recording(self) -> bytes:
        return b"".join(self.bursts)

    def summary(self) -> str:
        ber = "n/a" if self.ber_percent is None else f"{self.ber_percent:.1f}%"
        rssi = "n/a" if self.rssi_dbm is None else f"{self.rssi_dbm} dBm"
        return f"BER {ber}, RSSI {rssi}, loss {self.loss_percent:.1f}% ({self.lost}/{self.packets + self.lost})"


# ---- numbers and the spoken report ------------------------------------------------------------

_ES_TENS = {30: "30", 40: "40", 50: "50", 60: "60", 70: "70", 80: "80", 90: "90"}
_EN_TENS = {20: "20", 30: "30", 40: "40", 50: "50", 60: "60", 70: "70", 80: "80", 90: "90"}


def number_words(n: int, lang: str) -> list[str]:
    """0..199 as clip keys: ``er_n<k>`` for the number clips, ``er_and`` / ``er_hundred`` joiners."""
    if n < 0 or n > 199:
        raise ValueError(n)
    words: list[str] = []
    if n >= 100:
        if n == 100:
            return ["er_n100"]  # "cien" / "one hundred"
        words.append("er_hundred")  # "ciento" / "one hundred"
        if lang == "en":
            words.append("er_and")
        n -= 100
    if lang == "es":
        if n < 30:
            words.append(f"er_n{n}")
        else:
            tens, unit = (n // 10) * 10, n % 10
            words.append(f"er_n{_ES_TENS[tens]}")
            if unit:
                words += ["er_and", f"er_n{unit}"]
    else:
        if n < 20:
            words.append(f"er_n{n}")
        else:
            tens, unit = (n // 10) * 10, n % 10
            words.append(f"er_n{_EN_TENS[tens]}")
            if unit:
                words.append(f"er_n{unit}")
    return words


def decimal_words(value: float, lang: str) -> list[str]:
    """One decimal, "cero coma cuatro" / "zero point four"; whole numbers without the decimal."""
    tenths = round(value * 10)
    whole, frac = divmod(tenths, 10)
    words = number_words(min(whole, 199), lang)
    if frac:
        words += ["er_point", f"er_n{frac}"]
    return words


def report_words(session: EchoReportSession, lang: str) -> list[str]:
    """The report after the playback, as clip keys."""
    words: list[str] = []
    if session.ber_percent is None:
        words.append("er_nosignaldata")  # "sin datos de señal de radio" / "no radio signal data"
    else:
        words += ["er_ber"] + decimal_words(session.ber_percent, lang) + ["er_percent"]
        if session.rssi_dbm is not None:
            words += ["er_signal", "er_minus"] + number_words(min(-session.rssi_dbm, 199), lang) + ["er_dbm"]
    words += ["er_loss"] + decimal_words(session.loss_percent, lang) + ["er_percent"]
    return words


# Text of each clip, to generate them with TTS (tools/echo_report_clips.py).
def clip_texts(lang: str) -> dict[str, str]:
    if lang == "es":
        n = {i: t for i, t in enumerate(
            "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
            "diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro veinticinco "
            "veintiséis veintisiete veintiocho veintinueve".split())}
        n.update({30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta", 70: "setenta",
                  80: "ochenta", 90: "noventa", 100: "cien"})
        words = {"er_and": "y", "er_hundred": "ciento", "er_point": "coma", "er_percent": "por ciento",
                 "er_ber": "Tasa de error.", "er_signal": "Señal.", "er_minus": "menos", "er_dbm": "decibelios.",
                 "er_loss": "Pérdidas.", "er_nosignaldata": "Sin datos de señal de radio."}
    else:
        n = {i: t for i, t in enumerate(
            "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
            "sixteen seventeen eighteen nineteen".split())}
        n.update({20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy",
                  80: "eighty", 90: "ninety", 100: "one hundred"})
        words = {"er_and": "and", "er_hundred": "one hundred", "er_point": "point", "er_percent": "percent",
                 "er_ber": "Bit error rate.", "er_signal": "Signal.", "er_minus": "minus", "er_dbm": "d B m.",
                 "er_loss": "Packet loss.", "er_nosignaldata": "No radio signal data."}
    words.update({f"er_n{k}": v for k, v in n.items()})
    return words
