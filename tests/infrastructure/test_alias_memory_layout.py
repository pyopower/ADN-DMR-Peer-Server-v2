# ADN DMR Peer Server - one read, compact tables for the subscriber list
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

"""The subscriber list (300k entries) is read once and kept once.

Ids, profiles and local ids used to be three parses of the same file, and the
profiles a dict per subscriber; together ~390MB on the production list. What the
callers see must not change.
"""

from __future__ import annotations

import json
from pathlib import Path

from adn_server.infrastructure.persistence.alias_loader import (
    DefaultAliasLoader,
    SubscriberProfiles,
)

_RECORDS = [
    {"id": 7300391, "callsign": "CE5RPY", "fname": "Rodrigo", "surname": "Perez", "city": "x"},
    {"id": 7300392, "callsign": "CE5ABC", "fname": "Rodrigo", "surname": ""},
    {"id": 7300393, "callsign": "CE5TA", "talker_alias": "CE5TA Ana"},
    {"id": 7300394, "callsign": ""},
    {"id": "bad", "callsign": "NOPE"},
    {"callsign": "NOID"},
]


def _write(path: Path, name: str, records: list[dict]) -> Path:
    (path / name).write_text(json.dumps({"count": len(records), "results": records}))
    return path / name


def _cfg(path: Path, local: str = "subscriber_ids.json") -> dict:
    return {
        "ALIASES": {
            "PATH": str(path),
            "SUBSCRIBER_FILE": "subscriber_ids.json",
            "LOCAL_SUBSCRIBER_FILE": local,
            "PEER_FILE": "none.json",
            "TGID_FILE": "none.json",
            "SERVER_ID_FILE": "none.tsv",
        }
    }


def test_ids_and_profiles_read_as_before(tmp_path: Path) -> None:
    _write(tmp_path, "subscriber_ids.json", _RECORDS)
    loader = DefaultAliasLoader()
    subs = loader.load_aliases(_cfg(tmp_path))[1]
    profiles = loader.load_subscriber_profiles(_cfg(tmp_path))

    assert dict(subs) == {7300391: "CE5RPY", 7300392: "CE5ABC", 7300393: "CE5TA", 7300394: ""}
    assert profiles[7300391] == {"callsign": "CE5RPY", "fname": "Rodrigo", "surname": "Perez"}
    assert profiles.get(7300392) == {"callsign": "CE5ABC", "fname": "Rodrigo"}
    assert profiles[7300393] == {"callsign": "CE5TA", "talker_alias": "CE5TA Ana"}
    assert 7300394 not in profiles  # nothing to say about it: no profile, as before
    assert profiles.get(1, {}) == {}


def test_repeated_names_are_one_string(tmp_path: Path) -> None:
    rows = DefaultAliasLoader()._read_subscribers(_write(tmp_path, "s.json", _RECORDS)).profiles
    assert rows._rows[7300391][1] is rows._rows[7300392][1]


def test_the_usual_local_file_is_the_same_table_not_a_second_parse(tmp_path: Path) -> None:
    _write(tmp_path, "subscriber_ids.json", _RECORDS)
    loaded = DefaultAliasLoader().load_aliases(_cfg(tmp_path))
    assert loaded[3] is loaded[1]


def test_a_separate_local_file_still_overlays_the_profiles(tmp_path: Path) -> None:
    _write(tmp_path, "subscriber_ids.json", _RECORDS)
    _write(tmp_path, "local.json", [{"id": 7300391, "callsign": "CE5RPY", "surname": "Local"}])
    loader = DefaultAliasLoader()
    loaded = loader.load_aliases(_cfg(tmp_path, "local.json"))
    profiles = loader.load_subscriber_profiles(_cfg(tmp_path, "local.json"))

    assert loaded[3] == {7300391: "CE5RPY"}
    assert profiles[7300391] == {"callsign": "CE5RPY", "fname": "Rodrigo", "surname": "Local"}
    assert profiles[7300392] == {"callsign": "CE5ABC", "fname": "Rodrigo"}


def test_merge_adds_the_service_ids_without_copying_the_table(tmp_path: Path) -> None:
    _write(tmp_path, "subscriber_ids.json", _RECORDS)
    loader = DefaultAliasLoader()
    cfg = _cfg(tmp_path)
    loaded = loader.load_aliases(cfg)
    DefaultAliasLoader.merge_reload_into_config(cfg, loader, *loaded)

    assert cfg["_SUB_IDS"] is loaded[1]
    assert cfg["_SUB_IDS"][900999] == "D-APRS"
    assert cfg["_SUB_IDS"][4294967295] == "SC"
    assert isinstance(cfg["_SUB_PROFILES"], SubscriberProfiles)


def test_a_file_that_is_not_an_object_of_lists_gives_nothing(tmp_path: Path) -> None:
    (tmp_path / "s.json").write_text(json.dumps([{"id": 1, "callsign": "X"}]))
    loader = DefaultAliasLoader()
    assert loader._load_id_json(tmp_path / "s.json") == {}
    assert loader._read_subscribers(tmp_path / "s.json") == {}


def test_profiles_follow_the_ids_to_the_bak(tmp_path: Path) -> None:
    """A broken primary sends the ids to .bak; the profiles must not come from the broken file."""
    _write(tmp_path, "subscriber_ids.json", _RECORDS)
    loader = DefaultAliasLoader()
    cfg = _cfg(tmp_path)
    loader.load_aliases(cfg)

    _write(tmp_path, "subscriber_ids.json", [{"id": 9, "callsign": "HALF", "fname": "Written"}])
    (tmp_path / "subscriber_ids.json").write_text("{ truncated")
    subs = loader.load_aliases(cfg)[1]
    profiles = loader.load_subscriber_profiles(cfg)

    assert 7300391 in subs
    assert profiles[7300391]["fname"] == "Rodrigo"


def test_a_table_read_from_bak_is_dropped_once_the_primary_is_back(tmp_path: Path) -> None:
    _write(tmp_path, "subscriber_ids.json", _RECORDS)
    loader = DefaultAliasLoader()
    cfg = _cfg(tmp_path)
    loader.load_aliases(cfg)
    (tmp_path / "subscriber_ids.json").write_text("{ truncated")
    loader.load_aliases(cfg)
    bak = str(tmp_path / "subscriber_ids.json.bak")
    assert any(k[1] == bak for k in loader._by_content)

    _write(tmp_path, "subscriber_ids.json", _RECORDS[:1])
    loader.load_aliases(cfg)

    assert not any(k[1] == bak for k in loader._by_content)
