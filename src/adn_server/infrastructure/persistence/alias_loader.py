# ADN DMR Peer Server - alias loader
# Copyright (C) 2026  Rodrigo Pérez, CE5RPY <ce5rpy@qmd.cl>
#
# Derived from ADN DMR Server / FreeDMR  / HBlink. Original license:
###############################################################################
# Copyright (C) 2020 Simon Adlem, G7RZU <g7rzu@gb7fr.org.uk>
# Copyright (C) 2016-2019 Cortney T. Buffington, N0MJS <n0mjs@me.com>
#
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

"""Load alias dicts (peer_ids, subscriber_ids, talkgroup_ids, etc.). Legacy mk_aliases."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import shutil
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from ...application.ports import AliasLoader

logger = logging.getLogger(__name__)


def try_download(
    path: Path,
    file_name: str,
    url: str,
    stale_sec: float,
    *,
    max_attempts: int = 3,
    first_timeout: float = 30,
    retry_timeout: float = 10,
    retry_delay_sec: float = 3,
    expected_checksum: str | None = None,
    current_checksum: str | None = None,
) -> str:
    """Legacy try_download: download file from url if missing or older than stale_sec.

    When both the checksum the server publishes (`expected_checksum`) and the one of
    the file on disk (`current_checksum`) are known, they decide instead of the age:
    equal means the file is what the server has, however old; different means the
    server has published a new list, however recent ours is.

    Retries up to `max_attempts` times on network failure (timeout, connection refused,
    DNS failure, etc). The first attempt uses `first_timeout`; retries use the shorter
    `retry_timeout` so a fully unreachable host can't stall a caller for `max_attempts *
    first_timeout` (this runs synchronously during startup, and off-thread on the
    periodic reload — see alias_reload_loop in peer_server.py). Returns a result message.
    """
    if not url:
        return f"ID ALIAS MAPPER: '{file_name}' URL empty, not downloaded"
    full = path / file_name
    now = time.time()
    file_exists = full.is_file()
    if file_exists and expected_checksum and current_checksum:
        if current_checksum == expected_checksum:
            return f"ID ALIAS MAPPER: '{file_name}' is current, not downloaded"
    else:
        if file_exists:
            file_old = (full.stat().st_mtime + stale_sec) < now
        else:
            file_old = True
        if not file_old and file_exists:
            return f"ID ALIAS MAPPER: '{file_name}' is current, not downloaded"

    data: bytes | None = None
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        timeout = first_timeout if attempt == 1 else retry_timeout
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urlopen(url, context=ctx, timeout=timeout) as response:
                data = response.read()
            if expected_checksum and hashlib.blake2b(data).hexdigest() != expected_checksum:
                raise ValueError("downloaded data does not match expected checksum")
            last_error = None
            break
        except (OSError, ValueError) as e:
            last_error = e
            data = None
            if attempt < max_attempts:
                logger.warning(
                    "(ALIAS) ID ALIAS MAPPER: '%s' download attempt %d/%d failed (%s), retrying in %gs",
                    file_name, attempt, max_attempts, e, retry_delay_sec,
                )
                if retry_delay_sec:
                    time.sleep(retry_delay_sec)
    if isinstance(last_error, ValueError):
        return f"ID ALIAS MAPPER: '{file_name}' could not be downloaded, checksum mismatch after {max_attempts} attempts"
    if last_error is not None:
        return f"ID ALIAS MAPPER: '{file_name}' could not be downloaded due to an IOError: {last_error}"
    if not data or data == b"{}":
        return f"ID ALIAS MAPPER: '{file_name}' file not written because downloaded data is empty"
    if file_exists and full.stat().st_size == len(data) and full.read_bytes() == data:
        # Same bytes: rewriting would only give the file a new stamp, and a new
        # stamp makes the loader parse it all over again. Refresh the age only.
        os.utime(full)
        return f"ID ALIAS MAPPER: '{file_name}' downloaded, unchanged"
    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_name(f"{full.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_bytes(data)
            tmp.replace(full)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as e:
        return f"ID ALIAS mapper '{file_name}' file could not be written: {e}"
    return f"ID ALIAS MAPPER: '{file_name}' successfully downloaded"


_DOWNLOAD_FAILURE_MARKERS = (
    "could not be downloaded",
    "could not be written",
    "file not written because",
)


def _log_download_result(result: str) -> None:
    """Log a try_download result at a level a monitoring/alerting pipeline can filter on."""
    if any(marker in result for marker in _DOWNLOAD_FAILURE_MARKERS):
        logger.error("(ALIAS) %s", result)
    else:
        logger.info("(ALIAS) %s", result)


def _blake2bsum(file_path: Path) -> str:
    """Blake2b hex digest of file (legacy blake2bsum)."""
    h = hashlib.blake2b()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f"{dst.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        shutil.copy(src, tmp)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def _file_stamp(file_path: Path) -> tuple[int, int, int] | None:
    """Identifies a file's contents: try_download replaces, so the inode moves too."""
    try:
        stat = file_path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


_BAD_CHECKSUM_RETRY_SEC = 3600

_PROFILE_FIELDS = ("callsign", "fname", "surname", "talker_alias")

# IDs the server itself answers for; they are not in the published subscriber list.
SERVICE_SUBSCRIBER_IDS = {900999: "D-APRS", 4294967295: "SC"}


class SubscriberProfiles(Mapping):
    """{id: {callsign, fname, surname, talker_alias?}} kept as one tuple per id.

    A dict per subscriber costs ~5x the tuple, and there are 300k of them; the dict
    the callers expect is built only for the id they ask about.
    """

    __slots__ = ("_rows",)

    def __init__(self, rows: dict[int, tuple[str, str, str, str]] | None = None) -> None:
        self._rows = rows if rows is not None else {}

    def __getitem__(self, rid: int) -> dict[str, str]:
        row = self._rows[rid]
        return {k: v for k, v in zip(_PROFILE_FIELDS, row) if v}

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, rid: object) -> bool:
        return rid in self._rows

    def overlaid_with(self, other: SubscriberProfiles) -> SubscriberProfiles:
        """Legacy merge of two files: a non-empty field of `other` wins."""
        rows = dict(self._rows)
        for rid, row in other._rows.items():
            prev = rows.get(rid)
            rows[rid] = row if prev is None else tuple(n or p for n, p in zip(row, prev))
        return SubscriberProfiles(rows)


class SubscriberIds(dict):
    """{id: callsign} that also carries the profiles read in the same pass."""

    __slots__ = ("profiles",)

    def __init__(self) -> None:
        super().__init__()
        self.profiles = SubscriberProfiles()


def _scan_id_records(file_path: Path, on_record: Callable[[dict], None]) -> bool:
    """Feed every {"id": ...} record of an alias JSON file to `on_record`.

    json.load would first build the whole tree (a dict per record, ~8 fields each)
    and hold it next to what we keep; with 300k subscribers that tree is the peak
    of a reload. object_hook hands each record over as it is decoded and drops it.
    Returns False when the file can't be read or isn't the {"key": [records]} shape.
    """
    def hook(obj: dict) -> Any:
        if "id" in obj:
            on_record(obj)
            return None
        return obj

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f, object_hook=hook)
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(data, dict)


class DefaultAliasLoader(AliasLoader):
    """Load aliases from JSON files and optional downloads. Legacy mk_aliases.

    The reload loop ticks far more often than STALE_DAYS replaces the files, so
    parsed results are kept against each file's stamp.
    """

    def __init__(self) -> None:
        self._parsed: dict[str, tuple[Any, Any]] = {}
        # path -> (stamp, blake2b): a 44MB subscriber file takes ~0.1s to hash.
        self._digests: dict[str, tuple[tuple[int, int, int], str]] = {}
        # (parser, path) -> (blake2b, result): a download that brings the same bytes
        # gets a new stamp, but must not cost a parse.
        self._by_content: dict[tuple[str, str], tuple[str, Any]] = {}
        # file -> checksum the server published that the download didn't match.
        self._bad_downloads: dict[str, tuple[str, float]] = {}
        # (stamp of SUBSCRIBER_FILE, the ids load_aliases returned for it).
        self._chosen_subscribers: tuple[Any, dict[int, str]] | None = None

    def _digest(self, file_path: Path) -> str | None:
        stamp = _file_stamp(file_path)
        if stamp is None:
            return None
        key = str(file_path)
        known = self._digests.get(key)
        if known is not None and known[0] == stamp:
            return known[1]
        try:
            digest = _blake2bsum(file_path)
        except OSError:
            return None
        self._digests[key] = (stamp, digest)
        return digest

    def _forget(self, file_path: Path) -> None:
        key = str(file_path)
        self._digests.pop(key, None)
        for cached in [k for k in self._by_content if k[1] == key]:
            del self._by_content[cached]

    def _parse_by_content(self, kind: str, file_path: Path, parse: Callable[[Path], Any]) -> Any:
        digest = self._digest(file_path)
        key = (kind, str(file_path))
        known = self._by_content.get(key)
        if digest is not None and known is not None and known[0] == digest:
            return known[1]
        result = parse(file_path)
        if digest is not None:
            self._by_content[key] = (digest, result)
        else:
            self._by_content.pop(key, None)
        return result

    def load_aliases(
        self,
        config: dict[str, Any],
    ) -> tuple[
        dict[int, str],
        dict[int, str],
        dict[int, str],
        dict[int, str],
        dict[str, str],
        dict[str, str],
    ]:
        """Build alias dicts. Same order as legacy mk_aliases.

        With a checksum file configured, it is fetched on every tick (a few hundred
        bytes) and the lists are downloaded only when their published checksum no
        longer matches the file on disk: a DMR ID issued today is known within one
        poll of the server publishing it, and an unchanged 44MB list is never fetched
        again. Without one, STALE_DAYS decides as before.
        """
        aliases = config.get("ALIASES", {})
        path = Path(aliases.get("PATH", "./data/")).resolve()
        stale_sec = float(aliases.get("STALE_TIME", aliases.get("STALE_DAYS", 1) * 86400))
        if aliases.get("TRY_DOWNLOAD"):
            if aliases.get("CHECKSUM_FILE") and aliases.get("CHECKSUM_URL"):
                result = try_download(path, aliases["CHECKSUM_FILE"], aliases.get("CHECKSUM_URL", ""), 0)
                _log_download_result(result)
            checksums = self._load_checksums(path, aliases.get("CHECKSUM_FILE"))
            for key, url_key, checksum_key in [
                ("PEER_FILE", "PEER_URL", "peer_ids"),
                ("SUBSCRIBER_FILE", "SUBSCRIBER_URL", "subscriber_ids"),
                ("TGID_FILE", "TGID_URL", "talkgroup_ids"),
                ("SERVER_ID_FILE", "SERVER_ID_URL", "server_ids"),
            ]:
                url = aliases.get(url_key)
                if url and aliases.get(key):
                    self._download(path, aliases[key], url, stale_sec, checksums.get(checksum_key))
        else:
            checksums = self._load_checksums(path, aliases.get("CHECKSUM_FILE"))
        peer_file = aliases.get("PEER_FILE", "peer_ids.json")
        sub_file = aliases.get("SUBSCRIBER_FILE", "subscriber_ids.json")
        tgid_file = aliases.get("TGID_FILE", "talkgroup_ids.json")
        server_file = aliases.get("SERVER_ID_FILE", "server_ids.tsv")
        local_file = aliases.get("LOCAL_SUBSCRIBER_FILE", "subscriber_ids.json")
        peer_ids = self._load_with_backup(
            path, peer_file, checksums.get("peer_ids"), "peer_ids", self._load_id_json,
        )
        subscriber_ids = self._load_with_backup(
            path, sub_file, checksums.get("subscriber_ids"), "subscriber_ids",
            self._load_subscriber_json,
        )
        talkgroup_ids = self._load_with_backup(
            path, tgid_file, checksums.get("talkgroup_ids"), "talkgroup_ids", self._load_id_json,
        )
        if local_file == sub_file:
            # The usual setup: reading the same 300k subscribers a second time only
            # doubled the memory. The shared table also holds SERVICE_SUBSCRIBER_IDS,
            # which changes nothing: every lookup falls through to _SUB_IDS anyway.
            local_subscriber_ids = subscriber_ids
            # Whichever file the ids came from (.bak included), the profiles must too.
            self._chosen_subscribers = (_file_stamp(path / sub_file), subscriber_ids)
        else:
            local_subscriber_ids = self._parse_by_content(
                "ids", path / local_file, self._load_id_json,
            )
        server_ids = self._load_with_backup(
            path, server_file, checksums.get("server_ids"), "server_ids",
            lambda f: self._load_server_tsv(f.parent, f.name),
        )
        return (peer_ids, subscriber_ids, talkgroup_ids, local_subscriber_ids, server_ids, checksums)

    @staticmethod
    def merge_reload_into_config(
        config: dict[str, Any],
        alias_loader: AliasLoader,
        peer_ids: dict[int, str],
        subscriber_ids: dict[int, str],
        talkgroup_ids: dict[int, str],
        local_subscriber_ids: dict[int, str],
        server_ids: dict[str, str],
        checksums: dict[str, str],
        profiles: dict[int, dict[str, str]] | None = None,
    ) -> None:
        """Apply alias reload without wiping in-memory tables on partial download failure.

        This runs on the reactor thread, so ``profiles`` lets the caller build the
        300k of them off it instead.
        """
        def _keep(key: str, new_val: dict, label: str) -> None:
            if new_val:
                config[key] = new_val
            elif config.get(key):
                logger.warning(
                    "(ALIAS) reload kept previous %s (%d entries)",
                    label,
                    len(config[key]),
                )

        _keep("_PEER_IDS", peer_ids, "peer_ids")
        if subscriber_ids:
            # In place: the loader hands back the same table until the file changes,
            # and copying 300k entries every tick is what made an idle tick cost.
            subscriber_ids.update(SERVICE_SUBSCRIBER_IDS)
            config["_SUB_IDS"] = subscriber_ids
            if profiles is not None:
                config["_SUB_PROFILES"] = profiles
            elif isinstance(alias_loader, DefaultAliasLoader):
                config["_SUB_PROFILES"] = alias_loader.load_subscriber_profiles(config)
        elif config.get("_SUB_IDS"):
            logger.warning(
                "(ALIAS) reload kept previous subscriber_ids (%d entries)",
                len(config["_SUB_IDS"]),
            )
        _keep("_TG_IDS", talkgroup_ids, "talkgroup_ids")
        _keep("_LOCAL_SUBSCRIBER_IDS", local_subscriber_ids, "local_subscriber_ids")
        _keep("_SERVER_IDS", server_ids, "server_ids")
        if checksums:
            config["CHECKSUMS"] = checksums

    def _download(
        self, path: Path, file_name: str, url: str, stale_sec: float, expected: str | None,
    ) -> None:
        """try_download driven by the published checksum, with a retry limit for it.

        If the server publishes a checksum its own list doesn't match, every tick
        would fetch 44MB to throw it away; one attempt an hour per such checksum.
        """
        full = path / file_name
        current = self._digest(full) if expected else None
        bad = self._bad_downloads.get(file_name)
        if (
            bad is not None and expected == bad[0] and current != expected
            and time.time() - bad[1] < _BAD_CHECKSUM_RETRY_SEC
        ):
            logger.debug("(ALIAS) '%s': published checksum still unmatched, retry later", file_name)
            return
        result = try_download(
            path, file_name, url, stale_sec,
            expected_checksum=expected, current_checksum=current,
        )
        if expected and "checksum mismatch" in result:
            self._bad_downloads[file_name] = (expected, time.time())
        else:
            self._bad_downloads.pop(file_name, None)
        _log_download_result(result)

    def _load_checksums(self, path: Path, file_name: str | None) -> dict[str, str]:
        """Load checksum JSON (legacy load_json of CHECKSUM_FILE). Keys e.g. peer_ids, subscriber_ids, talkgroup_ids, server_ids."""
        if not file_name:
            return {}
        full = path / file_name
        if not full.is_file():
            return {}
        try:
            with open(full, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("(ALIAS) ID ALIAS MAPPER: Cannot load checksums: %s", e)
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def _load_server_tsv(self, path: Path, file_name: str) -> dict[str, str]:
        """Legacy mk_server_dict: TSV with 'OPB Net ID' -> 'Country'."""
        full = path / file_name
        if not full.is_file():
            return {}
        try:
            with open(full, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f, dialect="excel-tab")
                out: dict[str, str] = {}
                for row in reader:
                    net_id = row.get("OPB Net ID", "").strip()
                    country = row.get("Country", "").strip()
                    if net_id:
                        out[net_id] = country
                return out
        except Exception as err:
            logger.warning("(ALIAS) ID ALIAS MAPPER: %s could not be read: %s", file_name, err)
            return {}

    def _load_with_backup(
        self,
        path: Path,
        file_name: str,
        expected_checksum: str | None,
        name: str,
        parse: Callable[[Path], dict],
    ) -> dict:
        """Legacy mk_aliases load with .bak fallback, for whatever `parse` reads."""
        full = path / file_name
        bak = path / f"{file_name}.bak"
        stamp = _file_stamp(full)
        remembered = self._parsed.get(file_name)
        if stamp is not None and remembered is not None and remembered[0] == stamp:
            return remembered[1]
        result: dict = {}
        loaded_from_primary = False

        def _load_verified(target: Path) -> dict:
            if not target.is_file():
                # Raise (not return {}) so the caller falls back to .bak below instead of
                # silently ending up with an empty dictionary when the primary file is
                # simply missing (e.g. first boot with the download still failing).
                raise FileNotFoundError(f"'{target.name}' file does not exist")
            if expected_checksum:
                if self._digest(target) != expected_checksum:
                    raise ValueError("bad checksum")
            loaded = parse(target)
            if not loaded:
                raise ValueError("empty or invalid dictionary data")
            return loaded

        try:
            result = _load_verified(full)
            loaded_from_primary = True
        except Exception as e:
            self._parsed.pop(file_name, None)
            logger.error(
                "(ALIAS) ID ALIAS MAPPER: problem loading %s file (%s), falling back to .bak",
                name,
                e,
            )
            if bak.is_file():
                try:
                    result = parse(bak)
                except Exception as f:
                    logger.error(
                        "(ALIAS) ID ALIAS MAPPER: Tried backup %s file, but couldn't load that either: %s",
                        name,
                        f,
                    )
            else:
                logger.warning(
                    "(ALIAS) ID ALIAS MAPPER: no .bak available for %s, dictionary will be empty",
                    name,
                )
        if result:
            logger.info("(ALIAS) ID ALIAS MAPPER: %s dictionary is available", name)
        else:
            logger.warning("(ALIAS) ID ALIAS MAPPER: %s dictionary is empty", name)
        if loaded_from_primary:
            # A table parsed from .bak during an outage would otherwise stay cached
            # (the whole subscriber list) long after the primary came back.
            self._forget(bak)
        if loaded_from_primary and full.is_file():
            try:
                _atomic_copy(full, bak)
            except OSError as g:
                logger.info(
                    "(ALIAS) ID ALIAS MAPPER: couldn't make backup copy of %s file %s",
                    name,
                    g,
                )
            if stamp is not None:
                self._parsed[file_name] = (stamp, result)
        return result

    def _load_id_json(self, file_path: Path) -> dict[int, str]:
        """Load JSON with 'id' -> 'callsign' structure; return {int(id): callsign}."""
        out: dict[int, str] = {}

        def record(rec: dict) -> None:
            if "callsign" in rec:
                try:
                    out[int(rec["id"])] = str(rec["callsign"])
                except (ValueError, TypeError):
                    pass

        if not file_path.is_file() or not _scan_id_records(file_path, record):
            return {}
        return out

    def _load_subscriber_json(self, file_path: Path) -> SubscriberIds:
        """_load_id_json plus the profiles, in one read of the file.

        The ids, the profiles and the local ids used to be three separate parses
        of the same 44MB file, each keeping its own copy of every callsign.
        """
        return self._parse_by_content("subscribers", file_path, self._read_subscribers)

    @staticmethod
    def _read_subscribers(file_path: Path) -> SubscriberIds:
        out = SubscriberIds()
        rows: dict[int, tuple[str, str, str, str]] = {}
        # Names repeat a lot (fname "John", surname "Smith"...): one str per value.
        seen: dict[str, str] = {}

        def text(rec: dict, field: str) -> str:
            val = rec.get(field)
            if not val:
                return ""
            val = str(val)
            return seen.setdefault(val, val)

        def record(rec: dict) -> None:
            try:
                rid = int(rec["id"])
            except (ValueError, TypeError):
                return
            call = ""
            if "callsign" in rec:
                call = out[rid] = str(rec["callsign"])
            row = (
                call if rec.get("callsign") else "",
                text(rec, "fname"),
                text(rec, "surname"),
                text(rec, "talker_alias"),
            )
            if any(row):
                prev = rows.get(rid)
                rows[rid] = row if prev is None else tuple(n or p for n, p in zip(row, prev))

        if not file_path.is_file() or not _scan_id_records(file_path, record):
            return SubscriberIds()
        out.profiles = SubscriberProfiles(rows)
        return out

    def load_subscriber_profiles(self, config: dict[str, Any]) -> Mapping[int, dict[str, str]]:
        """Load {id: {callsign, fname, surname, talker_alias?}} from subscriber JSON files."""
        aliases = config.get("ALIASES", {})
        path = Path(aliases.get("PATH", "./data/")).resolve()
        sub_file = aliases.get("SUBSCRIBER_FILE", "subscriber_ids.json")
        local_file = aliases.get("LOCAL_SUBSCRIBER_FILE", "subscriber_ids.json")
        if local_file == sub_file:
            chosen = self._chosen_subscribers
            if chosen is not None and chosen[0] == _file_stamp(path / sub_file):
                return getattr(chosen[1], "profiles", SubscriberProfiles())
            return self._load_subscriber_json(path / sub_file).profiles
        profiles = self._load_subscriber_json(path / sub_file).profiles
        files = [path / sub_file, path / local_file]
        stamps = [_file_stamp(f) for f in files]
        remembered = self._parsed.get("_profiles")
        if remembered is not None and remembered[0] == stamps:
            return remembered[1]
        merged = profiles.overlaid_with(self._load_subscriber_json(path / local_file).profiles)
        self._parsed["_profiles"] = (stamps, merged)
        return merged
