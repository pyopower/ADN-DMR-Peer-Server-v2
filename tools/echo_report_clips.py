#!/usr/bin/env python3
# ADN DMR Peer Server - generate the echo report voice clips (Audio/<lang>/er_*.ambe)
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

"""Generate the clips of the echo report (private call to 9999) for one language.

    tools/echo_report_clips.py --lang es --piper /opt/piper/piper \
        --model /opt/piper/voices/es_ES-sharvard-medium.onnx --speaker 1 \
        --ambeserver 127.0.0.1:2460 --out Audio/es_ES

Text to speech with Piper, 8 kHz with sox, then AMBE+2 through an AMBEserver (a DV3000, or
md380-emu -s): each clip is written as ``er_<word>.ambe`` in the server's .ambe format (the 72-bit
AMBE frames back to back, padded with silence to whole voice bursts of 3 frames). The texts are
``clip_texts(lang)`` in ``adn_server/application/echo_report.py``.
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from adn_server.application.echo_report import clip_texts  # noqa: E402

SAMPLES = 160           # 20 ms at 8 kHz
FRAME_BYTES = SAMPLES * 2
AMBE72_BYTES = 9
FRAMES_PER_BURST = 3


def _packet(ptype: int, body: bytes) -> bytes:
    """AMBE3000 packet with the parity field (md380-emu -s enables parity)."""
    length = len(body) + 2
    header = bytes([0x61, (length >> 8) & 0xFF, length & 0xFF, ptype])
    parity = 0
    for b in header[1:] + body + b"\x2f":
        parity ^= b
    return header + body + bytes([0x2F, parity])


class AmbeServer:
    """One AMBEserver session for the whole run: md380-emu counts each UDP source port as a
    client and stops answering after its limit (-m 10), so a socket per clip doesn't work."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(4)
        self.sock.connect((host, port))
        self.sock.send(_packet(0x00, bytes([0x33])))  # reset
        r = self.sock.recv(512)
        if not (len(r) >= 5 and r[3] == 0x00 and r[4] == 0x39):
            raise RuntimeError(f"AMBEserver did not answer the reset: {r.hex()}")

    def encode(self, pcm: bytes) -> list[bytes]:
        """PCM s16le mono 8 kHz -> 72-bit AMBE frames (9 bytes each)."""
        frames = []
        for i in range(len(pcm) // FRAME_BYTES):
            chunk = pcm[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]
            be = b"".join(struct.pack(">h", v) for (v,) in struct.iter_unpack("<h", chunk))
            self.sock.send(_packet(0x02, bytes([0x00, SAMPLES]) + be))
            resp = self.sock.recv(512)
            if not (len(resp) >= 15 and resp[3] == 0x01 and resp[4] == 0x01 and resp[5] == 72):
                raise RuntimeError(f"unexpected AMBE answer at frame {i}: {resp.hex()}")
            frames.append(resp[6:6 + AMBE72_BYTES])
        return frames


def synth(text: str, a: argparse.Namespace) -> bytes:
    wav = subprocess.run(
        [a.piper, "-q", "-m", a.model, "-s", str(a.speaker), "--length_scale", str(a.length_scale), "-f", "-"],
        input=text.encode("utf-8"), check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    # 8 kHz mono s16, normalised, leading/trailing silence trimmed so the words join tightly
    return subprocess.run(
        ["sox", "-t", "wav", "-", "-t", "raw", "-r", "8000", "-e", "signed", "-b", "16", "-c", "1", "-",
         "gain", "-n", str(a.norm_db), "silence", "1", "0.02", "0.5%", "reverse", "silence", "1", "0.02", "0.5%",
         "reverse", "pad", "0", "0.06"],
        input=wav, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lang", required=True, choices=("es", "en"))
    ap.add_argument("--piper", default="/opt/piper/piper")
    ap.add_argument("--model", required=True)
    ap.add_argument("--speaker", default=0, type=int)
    ap.add_argument("--length-scale", default=1.0, type=float)
    ap.add_argument("--norm-db", default=-3.0, type=float)
    ap.add_argument("--ambeserver", default="127.0.0.1:2460")
    ap.add_argument("--out", required=True, help="the language's Audio directory, e.g. Audio/es_ES")
    a = ap.parse_args()
    host, port = a.ambeserver.rsplit(":", 1)
    os.makedirs(a.out, exist_ok=True)
    server = AmbeServer(host, int(port))
    silence = server.encode(b"\x00" * FRAME_BYTES)[0]
    for key, text in sorted(clip_texts(a.lang).items()):
        frames = server.encode(synth(text, a))
        frames += [silence] * (-len(frames) % FRAMES_PER_BURST)
        with open(os.path.join(a.out, key + ".ambe"), "wb") as f:
            f.write(b"".join(frames))
        print(f"{key}: {len(frames) // FRAMES_PER_BURST} bursts  ({text})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
