"""Fill in missing Steam vertical library covers (600x900) from SteamGridDB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from http.client import HTTPException, HTTPMessage
from pathlib import Path
from types import FrameType
from typing import IO, Callable, Dict, Iterable, List, Literal, Optional, Tuple, TypedDict, cast

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
STATE_FILE = HERE / "state.json"

KEY_PAGE = "https://www.steamgriddb.com/profile/preferences/api"
SGDB_API = "https://www.steamgriddb.com/api/v2"
STORE_API = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
CDN_URLS = [
    "https://shared.steamstatic.com/store_item_assets/steam/apps/{}/library_600x900.jpg",
    "https://steamcdn-a.akamaihd.net/steam/apps/{}/library_600x900.jpg",
]
USER_AGENT = "sgdb-covers/1.0"
NOT_FOUND_TTL = 30 * 24 * 3600
MAX_ERRORS_IN_A_ROW = 10
GRID_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# typing.* generics rather than builtins/`|` here: these aliases are evaluated at runtime,
# and the script supports Python 3.9.
AppInfo = Dict[str, Tuple[str, bool]]  # appid -> (app type, has real vertical art)
Written = Dict[str, Dict[str, str]]  # account -> appid -> sha1 of the cover file we wrote
Recorder = Callable[[Path, str], None]  # (grid folder, sha1) -> None
Result = Literal["applied", "official", "not_found"]
JsonDict = Dict[str, object]


class State(TypedDict):
    not_found: Dict[str, float]  # appid -> when SteamGridDB last had no cover for it
    written: Written


class Args(argparse.Namespace):
    dry_run: bool
    force: bool
    recheck: bool
    appid: Optional[List[str]]
    user: Optional[str]
    steam_path: Optional[str]
    workers: int
    adopt_existing: bool
    verbose: bool


class AuthError(Exception):
    pass


# ---------------------------------------------------------------- colors

def _supports_color() -> bool:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        # Turn on ANSI escape handling for the classic Windows console.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if not mode.value & 0x0004 and not kernel32.SetConsoleMode(handle, mode.value | 0x0004):
            return False
    return True


COLOR = _supports_color()


def paint(code: str) -> Callable[[object], str]:
    return lambda text: f"\033[{code}m{text}\033[0m" if COLOR else str(text)


bold, dim, red, green, yellow, cyan = (paint(c) for c in ("1", "2", "31", "32", "33", "36"))


# ---------------------------------------------------------------- .env / key

def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def save_env_key(key: str) -> None:
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = [l for l in ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
                 if not l.strip().startswith("SGDB_API_KEY=")]
    lines.append(f"SGDB_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def key_is_valid(key: str) -> bool:
    try:
        sgdb_get(key, "/grids/steam/70", {"dimensions": "600x900"})
        return True
    except AuthError:
        return False
    except Exception as e:
        sys.exit(red(f"Could not reach SteamGridDB to check the API key: {e}"))


def get_api_key() -> str:
    candidates: list[str] = []
    for k in (os.environ.get("SGDB_API_KEY"), read_env().get("SGDB_API_KEY")):
        if k and k not in candidates:
            candidates.append(k)
    for key in candidates:
        if key_is_valid(key):
            return key
    if candidates:
        print(yellow("The saved SteamGridDB API key was rejected."))
    else:
        print(yellow("No SteamGridDB API key found."))
    print(f"Get one here (log in, then 'Generate API Key'): {cyan(KEY_PAGE)}")
    while True:
        try:
            key = input(bold("Paste your SteamGridDB API key: ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
        if not key:
            continue
        if key_is_valid(key):
            save_env_key(key)
            print(green(f"Key saved to {ENV_FILE}"))
            return key
        print(red("That key was rejected by SteamGridDB, try again."))


# ---------------------------------------------------------------- HTTP

class KeepMethodRedirect(urllib.request.HTTPRedirectHandler):
    """urllib turns a redirected HEAD into a GET (downloading the body); keep it a HEAD."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: HTTPMessage, newurl: str) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and req.get_method() == "HEAD":
            new.method = "HEAD"
        return new


OPENER = urllib.request.build_opener(KeepMethodRedirect)


def retry_after(e: urllib.error.HTTPError, default: float) -> float:
    try:
        return min(max(float(e.headers.get("Retry-After", default)), 1), 120)
    except (TypeError, ValueError):
        return default


def http(url: str, method: str = "GET", headers: dict[str, str] | None = None,
         retries: int = 4) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            with OPENER.open(req, timeout=30) as r:
                status: int = r.status
                return status, r.read() if method != "HEAD" else b""
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < retries:
                wait_s = retry_after(e, delay) if e.code == 429 else delay
                e.close()
                time.sleep(wait_s)
                delay *= 2
                continue
            try:
                body = e.read() if method != "HEAD" else b""
            except (OSError, HTTPException):
                body = b""  # the status code is what matters; don't fail over an unreadable error body
            finally:
                e.close()
            return e.code, body
        except (OSError, HTTPException):  # URLError, timeouts, connection resets, IncompleteRead
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def as_dict(value: object) -> JsonDict:
    """Narrow a parsed JSON value to an object, or {} if it is anything else."""
    return cast(JsonDict, value) if isinstance(value, dict) else {}


def sgdb_get(key: str, path: str, params: dict[str, str]) -> list[JsonDict]:
    url = f"{SGDB_API}{path}?{urllib.parse.urlencode(params)}"
    status, body = http(url, headers={"Authorization": f"Bearer {key}"})
    if status == 401:
        raise AuthError()
    if status == 404:  # "Game not found"
        return []
    if status != 200:
        raise RuntimeError(f"SteamGridDB returned HTTP {status}")
    data = as_dict(json.loads(body))
    if not data.get("success"):
        raise RuntimeError(f"SteamGridDB error: {data.get('errors')}")
    items: object = data.get("data") or []
    if not isinstance(items, list):
        raise RuntimeError("unexpected SteamGridDB response")
    return [as_dict(item) for item in cast(List[object], items)]


# ---------------------------------------------------------------- Steam

def find_steam(override: str | None) -> Path:
    default = Path(r"C:\Program Files (x86)\Steam")
    if override:
        return Path(override)
    if sys.platform != "win32":
        return default
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(str(winreg.QueryValueEx(k, "SteamPath")[0]))
    except OSError:
        return default


def is_dead_link(path: Path) -> bool:
    """A symlink or junction whose target is gone (e.g. a moved Dropbox folder)."""
    return os.path.lexists(path) and not path.exists()


def find_grid_dirs(steam: Path, only_user: str | None, dry_run: bool) -> tuple[list[Path], list[str]]:
    """Returns (grid folders to use, account ids skipped because they have no grid folder)."""
    userdata = steam / "userdata"
    if only_user:
        if not only_user.isdigit() or not (userdata / only_user).is_dir():
            sys.exit(red(f"No Steam account folder {userdata / only_user}"))
        grid = userdata / only_user / "config" / "grid"
        if is_dead_link(grid):
            sys.exit(red(f"{grid} is a link to a folder that no longer exists. Fix or remove the link first."))
        if not dry_run:
            grid.mkdir(parents=True, exist_ok=True)
        return [grid], []
    if not userdata.is_dir():
        return [], []
    grids: list[Path] = []
    skipped: list[str] = []
    for d in sorted(userdata.iterdir()):
        if d.name.isdigit():
            grid = d / "config" / "grid"
            if grid.is_dir():
                grids.append(grid)
            else:
                skipped.append(d.name)
    return grids, skipped


LIBRARY_TYPES = {"game", "application", "demo"}  # app types that show a cover in the library


def read_appinfo(steam: Path) -> AppInfo:
    """Read Steam's local app database (appcache\\appinfo.vdf, binary KeyValues).

    Returns {appid: (type, has_real_capsule)}. Steam lists real vertical art under
    common/library_assets/library_capsule; apps without it only get the auto-generated
    placeholder. Returns {} if the file is missing or in an unknown format.
    """
    try:
        data = (steam / "appcache" / "appinfo.vdf").read_bytes()
        magic: int = struct.unpack_from("<I", data, 0)[0]
    except (OSError, struct.error):
        return {}
    if magic not in (0x07564427, 0x07564428, 0x07564429):
        return {}

    strings: list[str] | None = None
    off = 8
    if magic == 0x07564429:  # v29: keys are indexes into a string table at the end
        # Steam rewrites this file while running, so it may be cut short: treat a bad table as
        # unreadable rather than crash.
        try:
            table: int = struct.unpack_from("<q", data, 8)[0]
            count: int = struct.unpack_from("<I", data, table)[0]
            strings, pos = [], table + 4
            for _ in range(count):
                end = data.index(b"\0", pos)
                strings.append(data[pos:end].decode("utf-8", "replace"))
                pos = end + 1
        except (struct.error, ValueError):
            return {}
        off = 16

    def read_key(pos: int) -> tuple[str, int]:
        if strings is not None:
            index: int = struct.unpack_from("<I", data, pos)[0]
            return strings[index], pos + 4
        end = data.index(b"\0", pos)
        return data[pos:end].decode("utf-8", "replace"), end + 1

    def read_map(pos: int) -> tuple[JsonDict, int]:
        # Only strings and nested maps are kept; numbers are skipped (stored as None).
        out: JsonDict = {}
        while True:
            t = data[pos]
            pos += 1
            if t in (8, 11):
                return out, pos
            k, pos = read_key(pos)
            v: object
            if t == 0:
                v, pos = read_map(pos)
            elif t == 1:
                end = data.index(b"\0", pos)
                v, pos = data[pos:end].decode("utf-8", "replace"), end + 1
            elif t in (2, 3, 4, 6):
                v, pos = None, pos + 4
            elif t in (7, 10):
                v, pos = None, pos + 8
            else:
                raise ValueError(f"unknown type {t}")
            out[k] = v

    header = 4 + 4 + 8 + 20 + 4 + (20 if magic >= 0x07564428 else 0)  # state, time, token, sha1s, change#
    apps: AppInfo = {}
    try:
        while True:
            appid: int
            size: int
            appid, size = struct.unpack_from("<II", data, off)
            if appid == 0:
                break
            try:
                info, _ = read_map(off + 8 + header)
                common = as_dict(as_dict(info.get("appinfo")).get("common"))
                assets = as_dict(common.get("library_assets"))
                apps[str(appid)] = (str(common.get("type", "")).lower(), bool(assets.get("library_capsule")))
            except (ValueError, IndexError, struct.error):
                pass  # skip one unreadable entry
            off += 8 + size
    except struct.error:
        pass  # truncated file: keep what we read
    # Sanity check: a real library always has plenty of games with official art. If none do, the
    # format has probably changed (e.g. library_assets moved), and trusting it would make every
    # game look like it only has a placeholder.
    if len(apps) >= 100 and not any(art for _, art in apps.values()):
        return {}
    return apps


def has_cached_cover(cache_dir: Path) -> bool:
    # Older apps use library_600x900.jpg; newer ones use <hash>\library_capsule.jpg.
    return any(p.name.startswith(("library_600x900", "library_capsule")) for p in cache_dir.rglob("*"))


def is_placeholder(capsule: str) -> bool:
    # When a game has no real vertical art, Steam serves an auto-generated "portrait.png": the
    # wide header pasted on a blurred background. Real art is library_600x900.jpg or
    # <hash>/library_capsule.jpg.
    return capsule.rsplit("/", 1)[-1].split("?")[0].lower() == "portrait.png"


STORE_LIBRARY_TYPES = {0, 1, 6}  # store item types with a library cover: game, demo, software


def store_capsules(appids: Iterable[str]) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    """Ask the Steam store which apps have a real official vertical capsule.

    Returns (official, placeholder, unknown, failed, not_game): appids with real art; appids with
    only Steam's auto-generated placeholder; appids the store had no asset info for (delisted),
    which still need the CDN probe; appids whose request failed; and DLC, soundtracks etc.
    """
    def fetch(chunk: list[str]) -> tuple[list[str], list[object] | None]:
        req = {
            "ids": [{"appid": int(a)} for a in chunk],
            "context": {"language": "english", "country_code": "US"},
            "data_request": {"include_assets": True},
        }
        url = STORE_API + "?input_json=" + urllib.parse.quote(json.dumps(req))
        try:
            status, body = http(url)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            items: object = as_dict(json.loads(body)["response"]).get("store_items") or []
            if not isinstance(items, list):
                raise RuntimeError("unexpected response")
            return chunk, cast(List[object], items)
        except Exception:
            return chunk, None

    official: set[str] = set()
    placeholder: set[str] = set()
    answered: set[str] = set()
    failed: set[str] = set()
    not_game: set[str] = set()
    ids = list(appids)
    chunks = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, chunks))
    for chunk, items in results:
        if items is None:
            failed.update(chunk)
            continue
        for item in items:
            fields = as_dict(item)
            if "appid" not in fields:
                continue  # malformed item: its app stays "unknown" and gets the CDN probe
            appid, assets = str(fields["appid"]), fields.get("assets")
            if assets is None:
                continue
            answered.add(appid)
            capsule = as_dict(assets).get("library_capsule")
            item_type = fields.get("type")
            if isinstance(item_type, int) and item_type not in STORE_LIBRARY_TYPES:
                not_game.add(appid)
            elif capsule and is_placeholder(str(capsule)):
                placeholder.add(appid)
            elif capsule:
                official.add(appid)
    return official, placeholder, set(ids) - answered - failed, failed, not_game


def existing_custom(grid: Path, appid: str, listing: set[str] | None = None) -> list[Path]:
    """Custom covers for appid in grid. Pass `listing` (a set of lowercased file names, since
    Windows file names are case-insensitive) to avoid stat calls."""
    names = (f"{appid}p{ext}" for ext in GRID_EXTS)
    if listing is not None:
        return [grid / n for n in names if n in listing]
    return [grid / n for n in names if (grid / n).exists()]


def official_on_cdn(appid: str) -> bool:
    """True/False if the CDN gave a clear answer; raises if it couldn't be checked."""
    problems: list[str] = []
    clear_no = False
    for tpl in CDN_URLS:
        try:
            status, _ = http(tpl.format(appid), method="HEAD", retries=1)
        except Exception as e:
            problems.append(str(e))
            continue
        if status == 200:
            return True
        if status == 404:  # no such file (a 403 is more likely blocking/rate-limiting)
            clear_no = True
        else:
            problems.append(f"HTTP {status}")
    # One CDN clearly saying "no such file" is enough; only give up if none of them answered.
    if problems and not clear_no:
        raise RuntimeError(f"couldn't check for an official cover ({'; '.join(problems)})")
    return False


# ---------------------------------------------------------------- state

def load_state() -> State:
    try:
        raw = as_dict(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        raw = {}
    # Drop malformed and expired entries so the file doesn't grow forever.
    now = time.time()
    not_found = {a: float(ts) for a, ts in as_dict(raw.get("not_found")).items()
                 if isinstance(ts, (int, float)) and 0 <= now - ts < NOT_FOUND_TTL}
    # Covers this tool wrote: {account: {appid: sha1 of the file}}.
    written: Written = {acc: {a: h for a, h in as_dict(recs).items() if isinstance(h, str)}
                        for acc, recs in as_dict(raw.get("written")).items()}
    return {"not_found": not_found, "written": written}


def file_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def account_of(grid: Path) -> str:
    return grid.parent.parent.name  # userdata\<account>\config\grid


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


# Guards state changes made from worker threads (recording written covers) against a save in
# progress. Re-entrant because the Ctrl+C handler can save while the main thread holds it.
_state_lock = threading.RLock()


def save_state(state: State) -> None:
    """Save state.json; a failure is reported but doesn't abort the run (covers are already written)."""
    try:
        with _state_lock:
            data = json.dumps(state, indent=1, sort_keys=True).encode("utf-8")
        atomic_write(STATE_FILE, data)
    except OSError as e:
        print(yellow(f"Couldn't save {STATE_FILE.name}: {e}"))


def has_real_art(appinfo: AppInfo, appid: str) -> bool:
    return appinfo.get(appid, ("", False))[1]


# ---------------------------------------------------------------- work

# Cover writes in progress. A hard quit (second Ctrl+C) waits for these to finish, so it never
# leaves a cover moved aside without its replacement in place.
_writes = 0
_writes_lock = threading.Lock()
_stopping = False


def write_cover(grid: Path, appid: str, ext: str, data: bytes) -> None:
    global _writes
    with _writes_lock:
        if _stopping:
            raise RuntimeError("stopped before writing")
        _writes += 1
    try:
        _write_cover(grid, appid, ext, data)
    finally:
        with _writes_lock:
            _writes -= 1


def wait_for_writes(timeout: float = 10) -> None:
    """Block new cover writes and wait for the ones in progress to finish."""
    global _stopping
    with _writes_lock:
        _stopping = True
    deadline = time.time() + timeout
    while _writes and time.time() < deadline:
        time.sleep(0.05)


def _write_cover(grid: Path, appid: str, ext: str, data: bytes) -> None:
    """Replace <appid>p.* with <appid>p<ext>. If anything fails, the old cover(s) are restored."""
    target = grid / f"{appid}p{ext}"
    others = [p for p in existing_custom(grid, appid) if p != target]
    moved: list[tuple[Path, Path]] = []  # (backup, original) for covers with other extensions, moved aside
    try:
        for old in others:
            backup = grid / f".{old.name}.bak"
            os.replace(old, backup)
            moved.append((backup, old))
        atomic_write(target, data)  # also replaces a same-extension cover atomically
    except OSError as e:
        for backup, old in moved:
            try:
                os.replace(backup, old)
            except OSError:
                pass
        raise RuntimeError(f"couldn't write {target.name} (file in use?): {e}") from e
    for backup, _ in moved:
        try:
            backup.unlink()
        except OSError:
            pass  # leftover hidden .bak file; Steam ignores it


def retire_covers(grids: list[Path], written: Written, appinfo: AppInfo, dry_run: bool,
                  only: set[str] | None = None) -> list[tuple[str, str]]:
    """Remove covers this tool wrote for games that now have real official art.

    A cover is only removed if it is still exactly the file we wrote; if the user replaced or
    deleted it, it's no longer ours and its record is dropped. Returns [(appid, file name)].
    """
    retired: list[tuple[str, str]] = []
    for grid in grids:
        recs = written.get(account_of(grid), {})
        for appid, digest in list(recs.items()):
            if only is not None and appid not in only:
                continue
            files = existing_custom(grid, appid)
            if len(files) != 1:
                if not dry_run:
                    del recs[appid]  # gone, or the user added another: no longer ours
                continue
            if not has_real_art(appinfo, appid):
                continue  # still no real art: keep the cover
            try:
                ours = file_sha1(files[0]) == digest
            except OSError:
                continue
            if not ours:
                if not dry_run:
                    del recs[appid]  # the user swapped in their own cover
                continue
            if not dry_run:
                try:
                    files[0].unlink()
                except OSError as e:
                    print(red(f"Couldn't remove {files[0].name}: {e}"))
                    continue
                del recs[appid]
            retired.append((appid, files[0].name))
    return retired


def adopt_covers(grids: list[Path], written: Written, appinfo: AppInfo, only: set[str] | None = None) -> int:
    """Record untracked <appid>p.* covers as written by this tool. Returns how many.

    Only covers for games that currently have no real official art are adopted: those are the
    only ones this tool would have written. A custom cover for a game that already has official
    art is a deliberate choice, and must never be removed. (A cover set through Steam's own
    "Set Custom Artwork" uses the same file name and can't be told apart: check the dry run.)
    """
    adopted = 0
    for grid in grids:
        if not grid.is_dir():
            continue
        recs = written.setdefault(account_of(grid), {})
        for name in os.listdir(grid):
            stem, ext = os.path.splitext(name)
            appid = stem[:-1]
            if not (ext.lower() in GRID_EXTS and stem.lower().endswith("p") and appid.isdigit()):
                continue
            if (only is not None and appid not in only) or appid in recs:
                continue
            if appid not in appinfo or has_real_art(appinfo, appid):
                continue
            if len(existing_custom(grid, appid)) == 1:
                try:
                    recs[appid] = file_sha1(grid / name)
                except OSError as e:
                    print(yellow(f"Couldn't read {name}, not adopted: {e}"))
                    continue
                adopted += 1
    return adopted


def process(appid: str, key: str, grids: list[Path], args: Args, check_cdn: bool,
            record: Recorder | None = None) -> tuple[Result, str]:
    """Returns (result, detail). `grids` are the grid folders this app should be written to.

    record(grid, digest) is called right after each successful write (from this worker thread),
    so a cover is tracked even if the run is cut short or another grid's write fails.
    """
    if check_cdn and official_on_cdn(appid):
        return "official", ""
    grids_found = sgdb_get(key, f"/grids/steam/{appid}", {
        "dimensions": "600x900",
        "types": "static",
        "mimes": "image/png,image/jpeg",
        "nsfw": "false",
        "humor": "false",
    })
    if not grids_found:
        return "not_found", ""
    url = str(grids_found[0]["url"])
    if args.dry_run:
        return "applied", url
    status, data = http(url)
    if status != 200 or not data:
        raise RuntimeError(f"download failed (HTTP {status})")
    # Trust the file's contents, not the URL: never save an error page as a cover.
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    else:
        raise RuntimeError("download is not a PNG or JPEG image")
    digest = hashlib.sha1(data).hexdigest()
    failures: list[str] = []
    for grid in grids:
        try:
            write_cover(grid, appid, ext, data)
        except RuntimeError as e:
            failures.append(f"{account_of(grid)}: {e}")
            continue
        if record:
            record(grid, digest)
    if failures:
        raise RuntimeError("; ".join(failures))
    return "applied", url


def appid_arg(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"not a numeric appid: {value!r}")
    return str(int(value))


def workers_arg(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 32:
        raise argparse.ArgumentTypeError("must be between 1 and 32")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="show what would be downloaded; no covers or state are written")
    ap.add_argument("--force", action="store_true", help="replace existing custom covers too")
    ap.add_argument("--recheck", action="store_true", help="ignore the 'not on SteamGridDB' cache")
    ap.add_argument("--appid", nargs="+", type=appid_arg, help="only process these appids")
    ap.add_argument("--user", help="only this Steam userdata id")
    ap.add_argument("--steam-path", help="Steam install folder")
    ap.add_argument("--workers", type=workers_arg, default=6, help="parallel requests (1-32)")
    ap.add_argument("--adopt-existing", action="store_true",
                    help="treat existing covers for games without official art as written by this tool, "
                         "so they're removed once their game gets official art (run once after a fresh run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(namespace=Args())

    steam = find_steam(args.steam_path)
    cache = steam / "appcache" / "librarycache"
    if not args.appid and not cache.is_dir():
        sys.exit(red(f"Steam library cache not found: {cache} (use --steam-path to point at your Steam folder)"))
    grids, skipped_accounts = find_grid_dirs(steam, args.user, args.dry_run)
    dead = [acc for acc in skipped_accounts if is_dead_link(steam / "userdata" / acc / "config" / "grid")]
    for acc in dead:
        print(red(f"Account {acc}: its grid folder is a link to a folder that no longer exists "
                  f"({steam / 'userdata' / acc / 'config' / 'grid'}). Fix or remove the link."))
    if not grids:
        sys.exit(red("No usable userdata\\<id>\\config\\grid folder found"
                     + ("." if dead else " (use --user <id> to create one).")))
    print(f"{bold('Steam:')} {steam}")
    for g in grids:
        print(f"{bold('Grid folder:')} {g}")
    for acc in skipped_accounts:
        if acc not in dead:
            print(dim(f"Skipping account {acc}: it has no config\\grid folder (use --user {acc} to include it)."))

    state = load_state()
    not_found = state["not_found"]
    now = time.time()

    appids = args.appid or sorted((d.name for d in cache.iterdir() if d.is_dir() and d.name.isdigit()), key=int)
    explicit = bool(args.appid)
    counts = {"applied": 0, "official": 0, "placeholder": 0, "not_game": 0, "custom": 0,
              "not_found": 0, "skipped_nf": 0, "error": 0}
    # Explicit --appid with --force means "use SteamGridDB art even if Steam has an official cover".
    check_official = not (explicit and args.force)
    # Always read: besides deciding what needs a cover, it says which covers are safe to track.
    appinfo = read_appinfo(steam)
    if not appinfo:
        print(yellow("Couldn't read Steam's appinfo.vdf; falling back to the Steam store check."))

    written = state["written"]
    if args.dry_run:
        written = {acc: dict(recs) for acc, recs in written.items()}  # never touch real state
    only = set(appids) if explicit else None
    if args.adopt_existing:
        if not appinfo:
            sys.exit(red("--adopt-existing needs Steam's appinfo.vdf to tell which covers are safe to adopt."))
        n = adopt_covers(grids, written, appinfo, only)
        print(green(f"{'Would adopt' if args.dry_run else 'Adopted'} {n} existing cover{'' if n == 1 else 's'} "
                    f"(for games that only have Steam's placeholder)."))
    # Covers we wrote earlier whose game has since gotten real official art: remove them so the
    # official art shows. (Needs appinfo to know; without it nothing is removed.) Not with
    # --appid --force, which explicitly asks for SteamGridDB art over official art.
    retired = retire_covers(grids, written, appinfo, args.dry_run, only) if check_official else []
    retired_ids = {a for a, _ in retired}
    for appid, name in retired:
        print(f"{bold(appid)}: {cyan('official art now available,')} "
              f"{'would remove' if args.dry_run else 'removed'} {dim(name)}")
    if not args.dry_run and (args.adopt_existing or retired):
        save_state(state)  # now, in case the run stops before the end (e.g. at the key prompt)
    listings = {g: {n.lower() for n in os.listdir(g)} if g.is_dir() else set[str]() for g in grids}

    todo: dict[str, list[Path]] = {}  # appid -> grid folders to write to
    official: set[str] = set()
    undecided: dict[str, list[Path]] = {}  # apps appinfo doesn't know: decided by the store/CDN below
    for appid in dict.fromkeys(appids):
        targets = grids if args.force else [g for g in grids if not existing_custom(g, appid, listings[g])]
        meta = appinfo.get(appid) if check_official else None
        if appid in retired_ids:
            continue  # just handed back to its official art (reported above)
        if not targets:
            counts["custom"] += 1
            if explicit:
                print(dim(f"{appid} already has a custom cover (use --force to replace it)."))
        elif meta and meta[0] not in LIBRARY_TYPES and not explicit:
            counts["not_game"] += 1  # DLC, tools, soundtracks...: no cover in the library
        elif meta and meta[1]:
            official.add(appid)  # real art (Steam may just not have cached it yet)
        elif not explicit and not args.recheck and now - not_found.get(appid, 0) < NOT_FOUND_TTL:
            counts["skipped_nf"] += 1
        elif meta or not check_official:
            todo[appid] = targets
            counts["placeholder"] += bool(meta)  # no real art: Steam shows its generated placeholder
        else:
            undecided[appid] = targets

    unknown: set[str] = set()
    if undecided:
        # Not in appinfo. Ask the store, which tells real art from the placeholder (served as
        # portrait.png). A cached cover alone proves nothing, since the placeholder is cached too.
        print(dim(f"Checking {len(undecided)} apps against the Steam store..."))
        store_official, placeholder, unknown, failed, store_not_game = store_capsules(undecided)
        if not explicit:
            counts["not_game"] += len(store_not_game)
        # No answer from the store but a cover in Steam's cache: can't tell real art from a
        # placeholder, so keep it rather than risk replacing real art.
        keep = {a for a in unknown | failed if has_cached_cover(cache / a)}
        official |= store_official | keep
        unknown -= keep
        failed -= keep
        counts["placeholder"] += len(placeholder)
        if failed:
            # Without an answer we can't tell whether Steam has an official cover, so don't risk replacing it.
            counts["error"] += len(failed)
            print(red(f"Steam store check failed for {len(failed)} apps; skipping them this run."))
        todo.update({a: t for a, t in undecided.items()
                     if a not in official and a not in failed and (a not in store_not_game or explicit)})

    counts["official"] = len(official)
    if official and explicit:
        print(cyan(f"Official cover exists for {', '.join(sorted(official, key=int))} (use --force to override)."))

    print(bold(f"{len(todo)} apps need a cover from SteamGridDB."))
    key = get_api_key() if todo else ""

    def recorder(appid: str) -> Recorder | None:
        # Track a cover so it can be removed once its game gets official art. Never track one
        # forced over existing official art (--appid --force): that override is deliberate.
        # Apps appinfo doesn't list are only tracked when their official status was checked.
        if has_real_art(appinfo, appid) or (appid not in appinfo and not check_official):
            return None

        def record(grid: Path, digest: str) -> None:
            with _state_lock:
                written.setdefault(account_of(grid), {})[appid] = digest
        return record

    done = 0
    auth_failed = interrupted = False
    errors_in_a_row = 0  # stop early when everything fails (e.g. the connection dropped)

    def report(fut: Future[tuple[Result, str]], appid: str) -> None:
        nonlocal done, auth_failed, errors_in_a_row
        result: Result | Literal["auth", "error"]
        detail: object
        try:
            result, detail = fut.result()
        except AuthError:
            result, detail = "auth", None
        except Exception as e:
            result, detail = "error", e
        if result == "auth":
            auth_failed = True
        else:
            counts[result] += 1
        errors_in_a_row = errors_in_a_row + 1 if result == "error" else 0
        if result == "not_found":
            not_found[appid] = now
        elif result in ("applied", "official"):
            not_found.pop(appid, None)
        done += 1
        reported.add(fut)
        prefix = f"{dim(f'[{done}/{len(todo)}]')} {bold(appid)}:"
        if result == "error":
            print(f"{prefix} {red(f'error: {detail}')}")
        elif result == "applied":
            verb = "would apply" if args.dry_run else "applied"
            print(f"{prefix} {green(verb)} {dim(detail)}")
        elif result == "official" and args.verbose:
            print(f"{prefix} {cyan('official cover exists')}")
        elif result == "not_found" and args.verbose:
            print(f"{prefix} {yellow('not on SteamGridDB')}")

    def report_finished() -> None:
        for fut, appid in futures.items():
            if fut not in reported and fut.done() and not fut.cancelled():
                report(fut, appid)

    # Ctrl+C sets a flag instead of raising KeyboardInterrupt, so it can't land halfway through
    # recording a result. The loops below poll the flag (with short timeouts, since blocking
    # waits can't be interrupted on Windows).
    sigints = 0

    def on_sigint(signum: int, frame: FrameType | None) -> None:
        nonlocal sigints
        sigints += 1
        if sigints >= 2:
            # Quit now. Worker threads would be joined at interpreter exit, so leave hard,
            # but only once no cover is halfway through being replaced.
            wait_for_writes()
            if not args.dry_run:
                save_state(state)
            print(red("\nQuit."), flush=True)
            os._exit(130)

    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures: dict[Future[tuple[Result, str]], str] = {}
    reported: set[Future[tuple[Result, str]]] = set()
    old_handler = signal.signal(signal.SIGINT, on_sigint)
    try:
        for a, t in todo.items():
            futures[pool.submit(process, a, key, t, args, check_official and a in unknown, recorder(a))] = a
        pending = set(futures)
        while pending and not auth_failed and not sigints and errors_in_a_row < MAX_ERRORS_IN_A_ROW:
            finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in finished:
                report(fut, futures[fut])
        if sigints:
            interrupted = True
            print(yellow("\nInterrupted, finishing the downloads already in progress (Ctrl+C again to quit now)..."))
        elif errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
            print(red(f"\n{errors_in_a_row} errors in a row; stopping. Check your internet connection "
                      "and run again."))
        pool.shutdown(wait=False, cancel_futures=True)
        running = {f for f in futures if not f.done()}
        while running:
            _, running = wait(running, timeout=0.5)
        report_finished()  # downloads that were still running when we stopped
    finally:
        if not args.dry_run:
            save_state(state)  # while our handler is still active, so Ctrl+C can't cut it short
        signal.signal(signal.SIGINT, old_handler)
    not_processed = sum(1 for f in futures if f.cancelled())
    if auth_failed:
        print(red("\nSteamGridDB rejected the API key mid-run. Run again to check it or enter a new one."))

    print()
    print(green(f"{'Would apply' if args.dry_run else 'Applied'}: {counts['applied']}"))
    if retired:
        print(cyan(f"{'Would remove' if args.dry_run else 'Removed'} (game now has official art): {len(retired)}"))
    print(cyan(f"Has official art (left alone): {counts['official']}"))
    if counts["placeholder"]:
        print(dim(f"Only had Steam's auto-generated placeholder: {counts['placeholder']} (included above)"))
    print(dim(f"Already had a custom cover: {counts['custom']}"))
    print(dim(f"Not a game (DLC, tools, soundtracks...): {counts['not_game']}"))
    print(yellow(f"Not on SteamGridDB: {counts['not_found']}") + dim(f" (+{counts['skipped_nf']} skipped from earlier runs)"))
    print((red if counts["error"] else dim)(f"Errors: {counts['error']}"))
    if not_processed:
        print(yellow(f"Not processed (stopped early): {not_processed}"))
    if (counts["applied"] or retired) and not args.dry_run:
        print(bold("\nRestart Steam to see the new covers."))
    return 1 if (counts["error"] or auth_failed or interrupted) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl+C before any covers were being written (scanning, store check, key prompt).
        # Leave hard so background store requests aren't waited on.
        print(yellow("\nInterrupted."), flush=True)
        os._exit(130)
