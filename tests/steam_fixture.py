"""A fake Steam folder for tests: a library cache, an appinfo.vdf and one account's grid folder."""

import struct
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast


def encode_map(entries: Mapping[str, object], key: Callable[[str], bytes]) -> bytes:
    """Binary KeyValues holding maps, strings and ints; `key` writes each entry's name."""
    out = b""
    for name, value in entries.items():
        if isinstance(value, Mapping):
            out += b"\x00" + key(name) + encode_map(cast(Mapping[str, object], value), key)
        elif isinstance(value, str):
            out += b"\x01" + key(name) + value.encode() + b"\x00"
        elif isinstance(value, int):
            out += b"\x02" + key(name) + struct.pack("<i", value)
    return out + b"\x08"


def build_appinfo(apps: Mapping[int, Mapping[str, object]], version: int = 29, extra: bytes = b"") -> bytes:
    """An appinfo.vdf in format v29 (names in a string table at the end) or v28 (names inline).
    `extra` is raw bytes inserted after the apps, e.g. a damaged entry."""
    strings: list[str] = []

    def key(name: str) -> bytes:
        if version == 28:
            return name.encode() + b"\x00"
        if name not in strings:
            strings.append(name)
        return struct.pack("<I", strings.index(name))

    body = b""
    for appid, info in apps.items():
        entry = bytes(60) + encode_map({"appinfo": info}, key)  # 60: the entry header (state, sha1s...)
        body += struct.pack("<II", appid, len(entry)) + entry
    body += extra + struct.pack("<I", 0)
    if version == 28:
        return struct.pack("<II", 0x07564428, 1) + body
    table = struct.pack("<I", len(strings)) + b"".join(s.encode() + b"\x00" for s in strings)
    return struct.pack("<IIq", 0x07564429, 1, 16 + len(body)) + body + table


def app(name: str, kind: str = "Game", art: bool = False) -> dict[str, object]:
    """An appinfo.vdf entry: real vertical art if `art`, otherwise only Steam's placeholder."""
    common: dict[str, object] = {"type": kind, "name": name}
    if art:
        common["library_assets"] = {"library_capsule": "en"}
    return {"common": common}


def make_steam(steam: Path, apps: Mapping[int, Mapping[str, object]], covers: Mapping[str, bytes] | None = None) -> Path:
    """A Steam folder at `steam` with `apps` in its library cache and appinfo.vdf, and one account (1)
    whose grid folder holds `covers`. Returns the grid folder."""
    cache = steam / "appcache" / "librarycache"
    cache.mkdir(parents=True)
    for appid in apps:
        (cache / str(appid)).mkdir()
    (steam / "appcache" / "appinfo.vdf").write_bytes(build_appinfo(apps))
    grid = steam / "userdata" / "1" / "config" / "grid"
    grid.mkdir(parents=True)
    for name, data in (covers or {}).items():
        (grid / name).write_bytes(data)
    return grid
