"""Fill in missing Steam vertical library covers (600x900) from SteamGridDB."""

import sys

# Checked before the other imports, some of which older versions don't have.
if sys.version_info < (3, 11):
    sys.exit("sgdb-covers needs Python 3.11 or newer.")

import argparse
import hashlib
import io
import json
import os
import re
import signal
import ssl
import struct
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPMessage
from pathlib import Path
from types import FrameType
from typing import IO, Literal, TypedDict, TypeGuard, TypeVar, cast

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
STATE_FILE = HERE / "state.json"
LOCK_FILE = HERE / "sgdb-covers.lock"

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

# The covers this tool fetches. GRID_PARAMS asks SteamGridDB for exactly these, and check_grid()
# holds every answer to the same values, so a filter SteamGridDB stops applying can't slip through.
COVER_SIZE = (600, 900)
COVER_MIMES = ("image/png", "image/jpeg")
GRID_PARAMS = {
    "dimensions": f"{COVER_SIZE[0]}x{COVER_SIZE[1]}",
    "types": "static",
    "mimes": ",".join(COVER_MIMES),
    "nsfw": "false",
    "humor": "false",
    "limit": "1",  # results come top-voted first, and only that one is used
}
PROBE_APPID = "70"  # Half-Life has hundreds of covers: getting none for it means the API changed

Written = dict[str, dict[str, str]]  # account -> appid -> sha1 of the cover file we wrote
Recorder = Callable[[Path, str], None]  # (grid folder, sha1) -> None
Result = Literal["applied", "official", "not_found"]  # what process() did for one app
Outcome = Literal["applied", "official", "not_found", "error"]
JsonDict = dict[str, object]
_T = TypeVar("_T")


class State(TypedDict):
    not_found: dict[str, float]  # appid -> when SteamGridDB last had no cover for it
    written: Written


class Args(argparse.Namespace):
    dry_run: bool
    force: bool
    recheck: bool
    appid: list[str] | None
    user: str | None
    steam_path: str | None
    workers: int
    adopt_existing: bool
    verbose: bool


class AuthError(Exception):
    """SteamGridDB rejected the API key."""


class ApiError(Exception):
    """SteamGridDB answered in a way its API doesn't: most likely the API changed."""


@dataclass(frozen=True, slots=True)
class App:
    """An app in Steam's local app database."""
    kind: str  # "game", "dlc", "tool"... (lowercase)
    has_art: bool  # real vertical art, not just Steam's generated placeholder
    name: str


AppInfo = dict[str, App]


# ---------------------------------------------------------------- output

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


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def label(appid: str, names: dict[str, str]) -> str:
    """An app for messages: its id in bold, then its name when known."""
    name = names.get(appid)
    return f"{bold(appid)} {name}" if name else bold(appid)


def ago(when: float, now: float) -> str:
    days = int((now - when) // 86400)
    return "today" if days <= 0 else f"{plural(days, 'day')} ago"


def configure_output() -> None:
    # Output redirected to a file uses the system's legacy encoding, which lacks characters that some
    # game names, paths and Windows error messages have: print a stand-in instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


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
    """Save the key to .env, replacing an earlier one and keeping any other settings."""
    try:
        lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if ENV_FILE.exists() else []
        # Drop earlier keys the way read_env() reads them (it allows spaces around the "=").
        lines = [line for line in lines if line.split("=", 1)[0].strip() != "SGDB_API_KEY"]
        lines.append(f"SGDB_API_KEY={key}")
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        print(yellow(f"Couldn't save the key to {ENV_FILE}: {e}. You'll be asked for it again next time."))
    else:
        print(green(f"Key saved to {ENV_FILE}"))


def clean_key(raw: str) -> str:
    """The key without whitespace or invisible characters, like a zero-width space or byte-order mark
    copied along with it."""
    return "".join(c for c in raw if not c.isspace() and unicodedata.category(c) != "Cf")


def looks_like_key(key: str) -> bool:
    # Anything but plain printable ASCII couldn't even be sent in an HTTP header.
    return key.isascii() and key.isprintable()


def api_changed(e: ApiError) -> str:
    return (f"SteamGridDB answered unexpectedly ({e}); its API has probably changed. "
            "Stopping so no game is wrongly marked as having no art.")


def key_is_valid(key: str) -> bool:
    """Whether SteamGridDB accepts the key. Exits if SteamGridDB can't be reached, or if it answers in
    a way that suggests its API changed (every game would then look like it has no covers)."""
    if not looks_like_key(key):
        return False
    try:
        found = run_interruptibly(lambda: sgdb_get(key, f"/grids/steam/{PROBE_APPID}", GRID_PARAMS))
        if not found:
            raise ApiError("no covers for Half-Life, which has hundreds")
        check_grid(found[0])
    except AuthError:
        return False
    except ApiError as e:
        sys.exit(red(api_changed(e)))
    except (OSError, HTTPException, RuntimeError) as e:
        sys.exit(red(f"Could not reach SteamGridDB to check the API key: {e}"))
    return True


def get_api_key() -> str:
    candidates: list[str] = []
    for raw in (os.environ.get("SGDB_API_KEY"), read_env().get("SGDB_API_KEY")):
        key = clean_key(raw or "")
        if key and key not in candidates:
            candidates.append(key)
    for key in candidates:
        if key_is_valid(key):
            return key
    print(yellow("The saved SteamGridDB API key was rejected." if candidates else "No SteamGridDB API key found."))
    print(f"Get one here (log in, then 'Generate API Key'): {cyan(KEY_PAGE)}")
    while True:
        try:
            key = clean_key(input(bold("Paste your SteamGridDB API key: ")))
        except EOFError:  # no terminal to type into
            print()
            sys.exit(red(f"No API key entered. Run this in a terminal to paste one, or put "
                         f"SGDB_API_KEY=<your key> in {ENV_FILE} or the environment."))
        if not key:
            continue
        if not looks_like_key(key):
            print(red("That doesn't look like an API key, try again."))
        elif key_is_valid(key):
            save_env_key(key)
            return key
        else:
            print(red("That key was rejected by SteamGridDB, try again."))


# ---------------------------------------------------------------- HTTP

class RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows redirects like urllib does, except that a HEAD stays a HEAD (urllib turns it into a GET,
    downloading the body), and the API key isn't passed on to another site or over plain http."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: HTTPMessage, newurl: str) -> urllib.request.Request | None:
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if req.get_method() == "HEAD":
            new.method = "HEAD"
        old, target = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
        if target.hostname != old.hostname or (old.scheme == "https" and target.scheme != "https"):
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(RedirectHandler)


def retry_after(e: urllib.error.HTTPError, default: float) -> float:
    try:
        return min(max(float(e.headers.get("Retry-After", default)), 1), 120)
    except (TypeError, ValueError):
        return default


def certificate_problem(e: BaseException) -> ssl.SSLCertVerificationError | None:
    """The certificate failure behind a network error, if that's what it is (urllib wraps it)."""
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return reason if isinstance(reason, ssl.SSLCertVerificationError) else None


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
        except (OSError, HTTPException) as e:  # URLError, timeouts, connection resets, IncompleteRead
            if (problem := certificate_problem(e)) is not None:
                # Retrying can't help: something is intercepting secure connections.
                detail = getattr(problem, "verify_message", "") or problem
                raise RuntimeError(f"the secure connection to {urllib.parse.urlsplit(url).hostname} failed "
                                   f"its certificate check ({detail}); antivirus HTTPS scanning or a proxy?") from e
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def is_object(value: object) -> TypeGuard[JsonDict]:
    """Whether a parsed JSON value is an object (whose keys JSON makes strings)."""
    return isinstance(value, dict)


def as_dict(value: object) -> JsonDict:
    """A parsed JSON value as an object, or {} if it is anything else."""
    return value if is_object(value) else {}


def sgdb_get(key: str, path: str, params: dict[str, str]) -> list[JsonDict]:
    """GET from SteamGridDB's API. Returns [] for its documented "game not found" 404, and raises
    ApiError for any answer its API doesn't give."""
    url = f"{SGDB_API}{path}?{urllib.parse.urlencode(params)}"
    status, body = http(url, headers={"Authorization": f"Bearer {key}"})
    if status == 401:
        raise AuthError()
    try:
        data = as_dict(json.loads(body))
    except ValueError:  # not JSON: e.g. the HTML page a removed API version answers with
        data = {}
    if status == 404 and data.get("success") is False:
        return []  # "Could not find game with that id"
    if status in (400, 404):
        raise ApiError(f"HTTP {status} for {path}" + ("" if data else ", with a page that isn't JSON"))
    if status != 200:
        raise RuntimeError(f"SteamGridDB returned HTTP {status}")
    items = data.get("data")
    if data.get("success") is not True or not isinstance(items, list):
        raise ApiError(f"an answer for {path} without the usual fields")
    return [as_dict(item) for item in cast(list[object], items)]


def check_grid(grid: JsonDict) -> str:
    """The URL of a SteamGridDB result, once it's confirmed to be what GRID_PARAMS asked for."""
    url = grid.get("url")
    if ((grid.get("width"), grid.get("height")) != COVER_SIZE or grid.get("mime") not in COVER_MIMES
            or grid.get("nsfw") is not False or grid.get("humor") is not False
            or not isinstance(url, str) or not url.startswith("https://")):
        raise ApiError(f"cover {grid.get('id')} doesn't match the filters that were sent")
    return url


# ---------------------------------------------------------------- Steam

def steam_candidates() -> list[Path]:
    """Where Steam usually lives on this platform, most likely first."""
    if sys.platform == "win32":
        found: list[Path] = []
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                found.append(Path(str(winreg.QueryValueEx(k, "SteamPath")[0])))
        except OSError:
            pass
        return [*found, Path(r"C:\Program Files (x86)\Steam")]
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Steam"]
    return [
        home / ".steam" / "steam",  # a link to wherever Steam is installed
        home / ".local" / "share" / "Steam",
        home / ".steam" / "debian-installation",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",  # Flatpak
        home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
    ]


def find_steam(override: str | None) -> Path:
    if override:
        return Path(override)
    candidates = steam_candidates()
    return next((c for c in candidates if (c / "appcache").is_dir()), candidates[0])


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


# App types that get a cover in the library. Steam's app database lists playtests as "beta" and mods
# as games; "mod" is here for the store's type 2.
LIBRARY_TYPES = {"game", "application", "demo", "beta", "mod"}
# Steam store item types, named like the app database's types (each code checked against the store).
STORE_TYPES = {0: "game", 1: "demo", 2: "mod", 4: "dlc", 6: "application", 7: "video", 8: "series",
               10: "hardware", 11: "music", 12: "beta"}
MAX_DEPTH = 32  # real appinfo.vdf entries nest at most 6 levels deep; deeper means damaged data


def read_appinfo(steam: Path) -> AppInfo:
    """Read Steam's local app database (appcache\\appinfo.vdf, binary KeyValues).

    Returns {appid: App}. Steam lists real vertical art under common/library_assets/library_capsule;
    apps without it only get the auto-generated placeholder. Returns {} if the file is missing or in
    an unknown format.
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

    def read_map(pos: int, depth: int = 0) -> tuple[JsonDict, int]:
        # Only strings and nested maps are kept; numbers are skipped (stored as None).
        if depth > MAX_DEPTH:
            raise ValueError("nested too deep")
        out: JsonDict = {}
        while True:
            t = data[pos]
            pos += 1
            if t in (8, 11):
                return out, pos
            k, pos = read_key(pos)
            v: object
            if t == 0:
                v, pos = read_map(pos, depth + 1)
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
                kind, name = common.get("type"), common.get("name")
                apps[str(appid)] = App(kind.lower() if isinstance(kind, str) else "",
                                       bool(assets.get("library_capsule")),
                                       name if isinstance(name, str) else "")
            except (ValueError, IndexError, struct.error):
                pass  # skip one unreadable entry
            off += 8 + size
    except struct.error:
        pass  # truncated file: keep what we read
    # Sanity check: a real library always has plenty of games with official art. If none do, the
    # format has probably changed (e.g. library_assets moved), and trusting it would make every
    # game look like it only has a placeholder.
    if len(apps) >= 100 and not any(app.has_art for app in apps.values()):
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


@dataclass(frozen=True, slots=True)
class StoreCheck:
    """The Steam store's answer about apps that Steam's app database doesn't list."""
    official: set[str]  # real vertical art
    placeholder: set[str]  # only Steam's auto-generated placeholder
    unknown: set[str]  # no asset info (e.g. delisted): still needs the CDN probe
    failed: set[str]  # the request failed
    not_game: set[str]  # DLC, soundtracks etc.
    names: dict[str, str]


def store_capsules(appids: Iterable[str]) -> StoreCheck:
    """Ask the Steam store which apps have a real official vertical capsule."""
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
            return chunk, cast(list[object], items)
        except Exception:
            return chunk, None

    ids = list(appids)
    chunks = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futures = [pool.submit(fetch, chunk) for chunk in chunks]
        wait_all(futures)
    finally:
        # On Ctrl+C, don't wait for the requests still in flight: __main__ leaves right away.
        pool.shutdown(wait=False, cancel_futures=True)

    official: set[str] = set()
    placeholder: set[str] = set()
    answered: set[str] = set()
    failed: set[str] = set()
    not_game: set[str] = set()
    names: dict[str, str] = {}
    for future in futures:
        chunk, items = future.result()
        if items is None:
            failed.update(chunk)
            continue
        for item in items:
            fields = as_dict(item)
            if "appid" not in fields:
                continue  # malformed item: its app stays "unknown" and gets the CDN probe
            appid, assets, name = str(fields["appid"]), fields.get("assets"), fields.get("name")
            if isinstance(name, str) and name:
                names[appid] = name
            if assets is None:
                continue
            answered.add(appid)
            capsule = as_dict(assets).get("library_capsule")
            item_type = fields.get("type")
            if isinstance(item_type, int) and STORE_TYPES.get(item_type) not in LIBRARY_TYPES:
                not_game.add(appid)
            elif capsule and is_placeholder(str(capsule)):
                placeholder.add(appid)
            elif capsule:
                official.add(appid)
    return StoreCheck(official, placeholder, set(ids) - answered - failed, failed, not_game, names)


@dataclass(frozen=True, slots=True)
class Listing:
    """The file names in a folder, compared the way the folder's file system compares them."""
    names: frozenset[str]
    ignore_case: bool

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and (name.lower() if self.ignore_case else name) in self.names


def ignores_case(folder: Path, names: Iterable[str]) -> bool:
    """Whether the folder's file system matches names case-insensitively (as on Windows and usually
    macOS) or exactly (as on Linux). Steam's own lookups follow the file system, so ours must too."""
    for name in names:
        swapped = name.swapcase()
        # Only a name whose case swaps back cleanly is a fair test ("ß" becomes "SS", for one).
        if swapped != name and swapped.swapcase() == name and os.path.exists(folder / name):
            try:
                return os.path.samefile(folder / name, folder / swapped)
            except OSError:
                return False  # no file under the other spelling: names are matched exactly
    return sys.platform in ("win32", "darwin")  # nothing to test with: assume the platform's default


def list_folder(folder: Path) -> Listing:
    names = os.listdir(folder) if folder.is_dir() else []
    ignore_case = ignores_case(folder, names)
    return Listing(frozenset(n.lower() for n in names) if ignore_case else frozenset(names), ignore_case)


def existing_custom(grid: Path, appid: str, listing: Listing | None = None) -> list[Path]:
    """Custom covers for appid in grid. Pass the folder's `listing` to avoid a stat call per name."""
    names = [f"{appid}p{ext}" for ext in GRID_EXTS]
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

SHA1 = re.compile(r"[0-9a-f]{40}")


def empty_state() -> State:
    return {"not_found": {}, "written": {}}


def parse_state(raw: object, now: float) -> tuple[State, list[str], bool]:
    """State from parsed state.json, keeping everything valid.

    Returns (state, problems found, whether any record of a written cover was lost).
    """
    if not is_object(raw):
        return empty_state(), ["not a JSON object"], True
    problems: list[str] = []
    nf_raw, written_raw = raw.get("not_found", {}), raw.get("written", {})
    if not is_object(nf_raw):
        problems.append("'not_found' isn't an object")
    if not is_object(written_raw):
        problems.append("'written' isn't an object")
    # Games SteamGridDB had nothing for: malformed and expired entries are just dropped, which only
    # means looking the game up again.
    not_found = {a: float(ts) for a, ts in as_dict(nf_raw).items()
                 if isinstance(ts, (int, float)) and 0 <= now - ts < NOT_FOUND_TTL}
    # Covers this tool wrote: {account: {appid: sha1 of the file}}. Losing one of these means its cover
    # is never removed for official art, so every bad record is reported.
    written: Written = {}
    bad = 0
    for account, recs in as_dict(written_raw).items():
        if not account.isdigit() or not is_object(recs):
            bad += 1
            continue
        kept: dict[str, str] = {}
        for appid, digest in as_dict(recs).items():
            if appid.isdigit() and isinstance(digest, str) and SHA1.fullmatch(digest):
                kept[appid] = digest
            else:
                bad += 1
        written[account] = kept
    if bad:
        problems.append(f"malformed tracked-cover records: {bad}")
    lost = bad > 0 or not is_object(written_raw)
    return {"not_found": not_found, "written": written}, problems, lost


def load_state(dry_run: bool) -> State:
    """Read state.json. A missing file means a first run. An unreadable one stops the run, since
    overwriting it could lose the record of which covers this tool wrote. A damaged one is set aside
    with a warning, keeping whatever is still valid."""
    try:
        data = STATE_FILE.read_bytes()
    except FileNotFoundError:
        return empty_state()
    except OSError as e:
        sys.exit(red(f"Couldn't read {STATE_FILE} ({e}). It records which covers this tool wrote, so the "
                     "run stops rather than overwrite it."))
    try:
        state, problems, lost = parse_state(json.loads(data), time.time())
    except ValueError as e:  # not JSON, or not even text
        state, problems, lost = empty_state(), [f"not valid JSON ({e})"], True
    if problems:
        what = "; ".join(problems)
        kept = plural(sum(len(recs) for recs in state["written"].values()), "tracked cover")
        if dry_run:
            print(yellow(f"{STATE_FILE.name} is damaged ({what}). A real run would set it aside and keep {kept}."))
        else:
            backup = STATE_FILE.with_name(f"state-damaged-{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                os.replace(STATE_FILE, backup)
            except OSError as e:
                sys.exit(red(f"{STATE_FILE.name} is damaged ({what}) and couldn't be set aside: {e}"))
            print(yellow(f"{STATE_FILE.name} was damaged ({what}). Moved it to {backup.name} and kept {kept}."))
        if lost:
            print(yellow("Covers whose records were lost won't be removed when their game gets official art; "
                         "run --adopt-existing to track them again."))
    return state


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


# Guards state changes made from worker threads (recording written covers) against a save in progress.
_state_lock = threading.Lock()


def save_state(state: State) -> None:
    """Save state.json; a failure is reported but doesn't abort the run (covers are already written)."""
    try:
        with _state_lock:
            data = json.dumps(state, indent=1, sort_keys=True).encode("utf-8")
        atomic_write(STATE_FILE, data)
    except OSError as e:
        print(yellow(f"Couldn't save {STATE_FILE.name}: {e}. Covers this run wrote or adopted won't be "
                     "removed automatically once their game gets official art."))


def has_real_art(appinfo: AppInfo, appid: str) -> bool:
    app = appinfo.get(appid)
    return app is not None and app.has_art


def lock_file(path: Path) -> IO[bytes] | None:
    """Lock `path` for this process, or return None if another process holds it. The lock lasts while
    the returned file is open, and the OS drops it when the process ends, however it ends."""
    f = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def unlock_file(f: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


@contextmanager
def run_lock(dry_run: bool) -> Iterator[None]:
    """Keep a second run from starting while this one works. Dry runs write nothing, so they don't lock."""
    if dry_run:
        yield
        return
    try:
        lock = lock_file(LOCK_FILE)
    except OSError as e:
        sys.exit(red(f"Couldn't create {LOCK_FILE}: {e}"))
    if lock is None:
        sys.exit(red("Another sgdb-covers run is already in progress. Wait for it to finish."))
    try:
        yield
    finally:
        unlock_file(lock)


# ---------------------------------------------------------------- work

def wait_all(futures: Iterable[Future[_T]]) -> None:
    """Wait for futures in short steps: on Windows a blocking wait can't be interrupted by Ctrl+C."""
    pending = set(futures)
    while pending:
        _, pending = wait(pending, timeout=0.5)


def run_interruptibly(fn: Callable[[], _T]) -> _T:
    """Run fn on a worker thread, so a slow network call can't hold up Ctrl+C on the main thread."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(fn)
        wait_all([future])
        return future.result()
    finally:
        pool.shutdown(wait=False)


class WriteGuard:
    """Cover writes in progress. A hard quit (second Ctrl+C) waits for them, so it never leaves a cover
    moved aside without its replacement, or written but not recorded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self._stopped = False

    @contextmanager
    def writing(self) -> Iterator[None]:
        with self._lock:
            if self._stopped:
                raise RuntimeError("stopped before writing")
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1

    def stop_and_wait(self, timeout: float = 10) -> None:
        """Refuse new writes, and wait up to `timeout` seconds for the ones in progress."""
        with self._lock:
            self._stopped = True
        deadline = time.monotonic() + timeout
        while self._active and time.monotonic() < deadline:
            time.sleep(0.05)


WRITES = WriteGuard()


def write_cover(grid: Path, appid: str, ext: str, data: bytes) -> None:
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


def adopt_covers(grids: list[Path], written: Written, appinfo: AppInfo,
                 only: set[str] | None = None) -> list[tuple[str, str]]:
    """Record untracked <appid>p.* covers as written by this tool. Returns [(appid, file name)].

    Only covers for games that currently have no real official art are adopted: those are the
    only ones this tool would have written. A custom cover for a game that already has official
    art is a deliberate choice, and must never be removed. (A cover set through Steam's own
    "Set Custom Artwork" uses the same file name and can't be told apart: check the dry run.)
    """
    adopted: list[tuple[str, str]] = []
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
                adopted.append((appid, name))
    return adopted


def process(appid: str, key: str, grids: list[Path], dry_run: bool, check_cdn: bool,
            record: Recorder | None = None) -> tuple[Result, str]:
    """Returns (result, detail). `grids` are the grid folders this app should be written to.

    record(grid, digest) runs right after each successful write, as part of the same guarded step, so
    a cover is tracked even if the run is cut short or another grid's write fails.
    """
    if check_cdn and official_on_cdn(appid):
        return "official", ""
    found = sgdb_get(key, f"/grids/steam/{appid}", GRID_PARAMS)
    if not found:
        return "not_found", ""
    url = check_grid(found[0])
    if dry_run:
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
            with WRITES.writing():
                write_cover(grid, appid, ext, data)
                if record:
                    record(grid, digest)
        except RuntimeError as e:
            failures.append(f"{account_of(grid)}: {e}")
    if failures:
        raise RuntimeError("; ".join(failures))
    return "applied", url


# ---------------------------------------------------------------- run

@dataclass(frozen=True, slots=True)
class Folders:
    steam: Path
    cache: Path  # Steam's library cache: a folder per app
    grids: list[Path]  # the accounts' custom artwork folders


@dataclass(slots=True)
class Plan:
    """What happens to each app, decided before any lookup."""
    todo: dict[str, list[Path]] = field(default_factory=dict[str, list[Path]])  # appid -> grid folders
    undecided: dict[str, list[Path]] = field(default_factory=dict[str, list[Path]])  # the store decides
    official: set[str] = field(default_factory=set[str])
    custom: list[str] = field(default_factory=list[str])
    skipped: dict[str, float] = field(default_factory=dict[str, float])  # appid -> when SGDB had nothing
    not_game: int = 0
    placeholders: int = 0  # apps in todo that only have Steam's auto-generated placeholder
    store_failed: set[str] = field(default_factory=set[str])  # no answer from the store: skipped this run
    cdn_check: set[str] = field(default_factory=set[str])  # official status unknown: probe the CDN first


@dataclass(slots=True)
class LookupRun:
    """How the SteamGridDB lookups went."""
    counts: Counter[Outcome] = field(default_factory=Counter[Outcome])
    auth_failed: bool = False
    api_error: ApiError | None = None
    interrupted: bool = False
    not_processed: int = 0


def appid_arg(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError(f"not a numeric appid: {value!r}")
    return str(int(value))


def workers_arg(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 32:
        raise argparse.ArgumentTypeError("must be between 1 and 32")
    return n


def parse_args(argv: list[str] | None = None) -> Args:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="show what would be downloaded; no covers or state are written")
    ap.add_argument("--force", action="store_true", help="replace existing custom covers too")
    ap.add_argument("--recheck", action="store_true", help="ignore the 'not on SteamGridDB' cache")
    ap.add_argument("--appid", nargs="+", type=appid_arg, help="only process these appids")
    ap.add_argument("--user", help="only this Steam userdata id")
    ap.add_argument("--steam-path", help="Steam folder (found automatically when left out)")
    ap.add_argument("--workers", type=workers_arg, default=6, help="parallel requests (1-32)")
    ap.add_argument("--adopt-existing", action="store_true",
                    help="treat existing covers for games without official art as written by this tool, "
                         "so they're removed once their game gets official art (run once; add --dry-run "
                         "to see the list first)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also list each game left without a cover and why, and each adopted cover")
    return ap.parse_args(argv, namespace=Args())


def find_folders(args: Args) -> Folders:
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
        sys.exit(red(f"No usable {Path('userdata', '<id>', 'config', 'grid')} folder found"
                     + ("." if dead else " (use --user <id> to create one).")))
    print(f"{bold('Steam:')} {steam}")
    for g in grids:
        print(f"{bold('Grid folder:')} {g}")
    for acc in skipped_accounts:
        if acc not in dead:
            print(dim(f"Skipping account {acc}: it has no {Path('config', 'grid')} folder "
                      f"(use --user {acc} to include it)."))
    return Folders(steam, cache, grids)


def classify(appids: Iterable[str], grids: list[Path], listings: dict[Path, Listing], appinfo: AppInfo,
             not_found: dict[str, float], now: float, *, force: bool, explicit: bool, recheck: bool,
             check_official: bool, retired: set[str]) -> Plan:
    """Decide, for each app, whether it needs a lookup. Only reads what it's given."""
    plan = Plan()
    for appid in dict.fromkeys(appids):
        if appid in retired:
            continue  # just handed back to its official art (reported already)
        targets = grids if force else [g for g in grids if not existing_custom(g, appid, listings[g])]
        app = appinfo.get(appid) if check_official else None
        if not targets:
            plan.custom.append(appid)
        elif app and app.kind not in LIBRARY_TYPES and not explicit:
            plan.not_game += 1  # DLC, tools, soundtracks...: no cover in the library
        elif app and app.has_art:
            plan.official.add(appid)  # real art (Steam may just not have cached it yet)
        elif not explicit and not recheck and now - not_found.get(appid, 0) < NOT_FOUND_TTL:
            plan.skipped[appid] = not_found[appid]
        elif app or not check_official:
            plan.todo[appid] = targets
            if app:
                plan.placeholders += 1  # no real art: Steam shows its generated placeholder
        else:
            plan.undecided[appid] = targets
    return plan


def settle_with_store(plan: Plan, cache: Path, explicit: bool) -> StoreCheck:
    """Decide the apps Steam's app database doesn't list, by asking the Steam store, which tells real
    art from the placeholder (served as portrait.png). A cached cover alone proves nothing, since
    Steam caches the placeholder too."""
    check = store_capsules(plan.undecided)
    if not explicit:
        plan.not_game += len(check.not_game)
    # No answer from the store but a cover in Steam's cache: can't tell real art from a placeholder,
    # so keep it rather than risk replacing real art.
    keep = {a for a in check.unknown | check.failed if has_cached_cover(cache / a)}
    plan.official |= check.official | keep
    plan.placeholders += len(check.placeholder)
    plan.store_failed = check.failed - keep
    plan.cdn_check = check.unknown - keep
    plan.todo.update({a: t for a, t in plan.undecided.items()
                      if a not in plan.official and a not in plan.store_failed
                      and (a not in check.not_game or explicit)})
    return check


def run_lookups(plan: Plan, key: str, args: Args, state: State, names: dict[str, str],
                recorder: Callable[[str], Recorder | None], save: Callable[[], None]) -> LookupRun:
    """Look up and apply covers for plan.todo in parallel, printing each result.

    Ctrl+C only sets a flag, so it can't land halfway through recording a result: no new lookups
    start, and the ones in progress finish. A second Ctrl+C quits at once, as soon as no cover is
    mid-write. `save` runs at the end (and on that quit) with Ctrl+C ignored, so it can't be cut short.
    """
    lookups = LookupRun()
    not_found = state["not_found"]
    now = time.time()
    done = errors_in_a_row = sigints = 0
    futures: dict[Future[tuple[Result, str]], str] = {}
    reported: set[Future[tuple[Result, str]]] = set()

    def report(fut: Future[tuple[Result, str]], appid: str) -> None:
        nonlocal done, errors_in_a_row
        outcome: Outcome | None
        detail = ""
        try:
            outcome, detail = fut.result()
        except AuthError:
            lookups.auth_failed, outcome = True, None
        except ApiError as e:
            lookups.api_error, outcome = lookups.api_error or e, None
        except Exception as e:
            outcome, detail = "error", str(e)
        done += 1
        reported.add(fut)
        errors_in_a_row = errors_in_a_row + 1 if outcome == "error" else 0
        if outcome is None:
            return  # reported once, after the run stops
        lookups.counts[outcome] += 1
        if outcome == "not_found":
            not_found[appid] = now
        elif outcome in ("applied", "official"):
            not_found.pop(appid, None)
        prefix = f"{dim(f'[{done}/{len(plan.todo)}]')} {label(appid, names)}:"
        if outcome == "error":
            print(f"{prefix} {red(f'error: {detail}')}")
        elif outcome == "applied":
            print(f"{prefix} {green('would apply' if args.dry_run else 'applied')} {dim(detail)}")
        elif outcome == "official" and args.verbose:
            print(f"{prefix} {cyan('official cover exists')}")
        elif outcome == "not_found" and args.verbose:
            print(f"{prefix} {yellow('not on SteamGridDB')}")

    def on_sigint(signum: int, frame: FrameType | None) -> None:
        nonlocal sigints
        sigints += 1
        if sigints >= 2:
            # Quit now. Worker threads would be joined at interpreter exit, so leave hard, but only once
            # no cover is halfway through being replaced. More Ctrl+C can't make this any faster.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            WRITES.stop_and_wait()
            if not args.dry_run:
                save()
            print(red("\nQuit."), flush=True)
            os._exit(130)

    pool = ThreadPoolExecutor(max_workers=args.workers)
    old_handler = signal.signal(signal.SIGINT, on_sigint)
    try:
        for appid, targets in plan.todo.items():
            fut = pool.submit(process, appid, key, targets, args.dry_run, appid in plan.cdn_check, recorder(appid))
            futures[fut] = appid
        pending = set(futures)
        while (pending and not (sigints or lookups.auth_failed or lookups.api_error)
               and errors_in_a_row < MAX_ERRORS_IN_A_ROW):
            finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for fut in finished:
                report(fut, futures[fut])
        if sigints:
            lookups.interrupted = True
            print(yellow("\nInterrupted, finishing the downloads already in progress (Ctrl+C again to quit now)..."))
        elif errors_in_a_row >= MAX_ERRORS_IN_A_ROW:
            print(red(f"\n{errors_in_a_row} errors in a row; stopping. Check your internet connection "
                      "and run again."))
        pool.shutdown(wait=False, cancel_futures=True)
        wait_all(f for f in futures if not f.done())
        for fut, appid in futures.items():  # lookups that were still running when the loop stopped
            if fut not in reported and fut.done() and not fut.cancelled():
                report(fut, appid)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # the save mustn't be cut short
        try:
            if not args.dry_run:
                save()
        finally:
            signal.signal(signal.SIGINT, old_handler if old_handler is not None else signal.default_int_handler)
    lookups.not_processed = sum(1 for f in futures if f.cancelled())
    if lookups.auth_failed:
        print(red("\nSteamGridDB rejected the API key mid-run. Run again to check it or enter a new one."))
    if lookups.api_error:
        print(red(f"\n{api_changed(lookups.api_error)}"))
    return lookups


def print_summary(plan: Plan, lookups: LookupRun, retired: int, dry_run: bool) -> None:
    print()
    print(green(f"{'Would apply' if dry_run else 'Applied'}: {lookups.counts['applied']}"))
    if retired:
        print(cyan(f"{'Would remove' if dry_run else 'Removed'} (game now has official art): {retired}"))
    print(cyan(f"Has official art (left alone): {len(plan.official) + lookups.counts['official']}"))
    print(dim(f"Already had a custom cover: {len(plan.custom)}"))
    print(dim(f"Not a game (DLC, tools, soundtracks...): {plan.not_game}"))
    print(yellow(f"Not on SteamGridDB: {lookups.counts['not_found']}")
          + dim(f" (+{len(plan.skipped)} skipped from earlier runs)"))
    errors = lookups.counts["error"] + len(plan.store_failed)
    print((red if errors else dim)(f"Errors: {errors}"))
    if lookups.not_processed:
        print(yellow(f"Not processed (stopped early): {lookups.not_processed}"))
    if (lookups.counts["applied"] or retired) and not dry_run:
        print(bold("\nRestart Steam to see the new covers."))


def run(args: Args, folders: Folders) -> int:
    """Everything after finding the folders (and, for a real run, taking the run lock)."""
    state = load_state(args.dry_run)
    now = time.time()
    appids = args.appid or sorted(
        (d.name for d in folders.cache.iterdir() if d.is_dir() and d.name.isdigit()), key=int)
    explicit = bool(args.appid)
    # Explicit --appid with --force means "use SteamGridDB art even if Steam has an official cover".
    check_official = not (explicit and args.force)
    # Always read: besides deciding what needs a cover, it says which covers are safe to track.
    appinfo = read_appinfo(folders.steam)
    if not appinfo:
        print(yellow("Couldn't read Steam's appinfo.vdf; falling back to the Steam store check."))
    names = {appid: app.name for appid, app in appinfo.items() if app.name}

    written = state["written"]
    if args.dry_run:
        written = {acc: dict(recs) for acc, recs in written.items()}  # never touch real state
    only = set(appids) if explicit else None
    if args.adopt_existing:
        if not appinfo:
            sys.exit(red("--adopt-existing needs Steam's appinfo.vdf to tell which covers are safe to adopt."))
        adopted = adopt_covers(folders.grids, written, appinfo, only)
        if args.dry_run or args.verbose:
            for appid, name in sorted(adopted, key=lambda item: int(item[0])):
                print(f"{label(appid, names)}: {'would adopt' if args.dry_run else 'adopted'} {dim(name)}")
        print(green(f"{'Would adopt' if args.dry_run else 'Adopted'} {plural(len(adopted), 'existing cover')} "
                    "(for games that only have Steam's placeholder)."))
    # Covers we wrote earlier whose game has since gotten real official art: remove them so the
    # official art shows. (Needs appinfo to know; without it nothing is removed.) Not with
    # --appid --force, which explicitly asks for SteamGridDB art over official art.
    retired = retire_covers(folders.grids, written, appinfo, args.dry_run, only) if check_official else []
    for appid, name in retired:
        print(f"{label(appid, names)}: {cyan('official art now available,')} "
              f"{'would remove' if args.dry_run else 'removed'} {dim(name)}")
    if not args.dry_run and (args.adopt_existing or retired):
        save_state(state)  # now, in case the run stops before the end (e.g. at the key prompt)

    listings = {g: list_folder(g) for g in folders.grids}
    plan = classify(appids, folders.grids, listings, appinfo, state["not_found"], now, force=args.force,
                    explicit=explicit, recheck=args.recheck, check_official=check_official,
                    retired={appid for appid, _ in retired})
    if explicit:
        for appid in plan.custom:
            print(f"{label(appid, names)} {dim('already has a custom cover (use --force to replace it).')}")
    if args.verbose:
        for appid, when in sorted(plan.skipped.items(), key=lambda item: int(item[0])):
            print(f"{label(appid, names)}: {yellow('no art on SteamGridDB')} "
                  f"{dim(f'(checked {ago(when, now)}; --recheck to look again)')}")
    if plan.undecided:
        print(dim(f"Checking {plural(len(plan.undecided), 'app')} against the Steam store..."))
        store = settle_with_store(plan, folders.cache, explicit)
        names.update({appid: name for appid, name in store.names.items() if appid not in names})
        if plan.store_failed:
            # Without an answer we can't tell whether Steam has an official cover, so don't risk replacing it.
            print(red(f"Steam store check failed for {plural(len(plan.store_failed), 'app')}; skipping them this run."))
            if args.verbose:
                for appid in sorted(plan.store_failed, key=int):
                    print(f"{label(appid, names)}: {red('the Steam store check failed; skipped this run')}")
    if explicit and plan.official:
        print(cyan("Official cover exists for ") + ", ".join(label(a, names) for a in sorted(plan.official, key=int))
              + cyan(" (use --force to override)."))

    if plan.todo:
        note = f" ({plan.placeholders} with only Steam's auto-generated placeholder)" if plan.placeholders else ""
        print(bold(f"{plural(len(plan.todo), 'app')} to look up on SteamGridDB{note}."))
    else:
        print(bold("No apps need a cover from SteamGridDB."))
    key = get_api_key() if plan.todo else ""

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

    lookups = run_lookups(plan, key, args, state, names, recorder, lambda: save_state(state))
    print_summary(plan, lookups, len(retired), args.dry_run)
    errors = lookups.counts["error"] + len(plan.store_failed)
    return 1 if (errors or lookups.auth_failed or lookups.api_error or lookups.interrupted) else 0


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = parse_args(argv)
    folders = find_folders(args)
    with run_lock(args.dry_run):
        return run(args, folders)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl+C before any cover is being written (scanning, store check, key prompt). Leave at once:
        # requests still in flight run on worker threads, which a normal exit would wait for.
        print(yellow("\nInterrupted."), flush=True)
        os._exit(130)
