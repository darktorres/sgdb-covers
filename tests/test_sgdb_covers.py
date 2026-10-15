"""Tests for sgdb_covers. Standard library only, and no network: run with `python -m unittest`."""

import _thread
import contextlib
import hashlib
import io
import json
import os
import ssl
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from http.client import HTTPMessage
from pathlib import Path
from unittest import mock

os.environ["NO_COLOR"] = "1"  # before the import, which decides on colors
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sgdb_covers as sc  # noqa: E402
from tests.steam_fixture import app, build_appinfo, make_steam  # noqa: E402

REAL_HTTP = sc.http  # every test replaces sc.http; the certificate test needs the real one
PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)
JPEG = b"\xff\xd8\xff" + bytes(32)
KEY = "0123456789abcdef0123456789abcdef"
NOW = 1_700_000_000.0

Response = tuple[int, bytes]
Route = Response | Callable[[], Response]


def grid(**changes: object) -> dict[str, object]:
    """A SteamGridDB result matching what GRID_PARAMS asks for, with `changes` applied."""
    result: dict[str, object] = {"id": 1, "url": "https://cdn.example/cover.png", "width": 600,
                                 "height": 900, "mime": "image/png", "nsfw": False, "humor": False}
    result.update(changes)
    return result


def grids(*results: dict[str, object]) -> Response:
    return 200, json.dumps({"success": True, "data": list(results)}).encode()


GAME_NOT_FOUND: Response = (404, json.dumps({"success": False, "errors": ["Game not found"]}).encode())
HTML_404: Response = (404, b"<!DOCTYPE html><html><body>Not found</body></html>")
PROBE = {"/grids/steam/70?": grids(grid())}  # Half-Life, checked along with the API key


class FakeHttp:
    """Stands in for sc.http: answers by URL substring, and fails on any request it wasn't told about."""

    def __init__(self, routes: Mapping[str, Route]) -> None:
        self.routes = dict(routes)
        self.calls: list[str] = []

    def __call__(self, url: str, method: str = "GET", headers: dict[str, str] | None = None,
                 retries: int = 4) -> Response:
        self.calls.append(url)
        for part, answer in self.routes.items():
            if part in url:
                return answer() if callable(answer) else answer
        raise AssertionError(f"unexpected request: {url}")


class Case(unittest.TestCase):
    """A test with its own folder for state.json, .env and the run lock, and no real network."""

    steam: Path

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.patch(sc, "STATE_FILE", self.tmp / "state.json")
        self.patch(sc, "ENV_FILE", self.tmp / ".env")
        self.patch(sc, "LOCK_FILE", self.tmp / "sgdb-covers.lock")
        self.patch(sc, "WRITES", sc.WriteGuard())
        self.routes({})
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("SGDB_API_KEY", None)

    def patch(self, target: object, name: str, value: object) -> None:
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def routes(self, routes: Mapping[str, Route]) -> FakeHttp:
        fake = FakeHttp(routes)
        self.patch(sc, "http", fake)
        return fake

    def make_steam(self, apps: Mapping[int, Mapping[str, object]], covers: Mapping[str, bytes] | None = None) -> Path:
        """A Steam folder with `apps`, and one account (1) whose grid folder holds `covers`. Returns the
        grid folder."""
        self.steam = self.tmp / "Steam"
        return make_steam(self.steam, apps, covers)

    def save_key(self) -> None:
        sc.ENV_FILE.write_text(f"SGDB_API_KEY={KEY}\n", encoding="utf-8")

    def run_main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sc.main([*argv, "--steam-path", str(self.steam)])
        return code, out.getvalue()


# ---------------------------------------------------------------- 1. SteamGridDB API changes

class TestSteamGridDBAnswers(Case):
    def get(self, answer: Response) -> list[sc.JsonDict]:
        self.routes({"/grids/steam/10?": answer})
        return sc.sgdb_get(KEY, "/grids/steam/10", sc.GRID_PARAMS)

    def test_game_not_found_is_an_empty_answer(self) -> None:
        self.assertEqual(self.get(GAME_NOT_FOUND), [])

    def test_answers_the_api_never_gives_raise_api_error(self) -> None:
        for answer in (HTML_404, (400, b'{"success": false}'), (200, b'{"data": []}'), (200, b"<html></html>")):
            with self.subTest(answer=answer), self.assertRaises(sc.ApiError):
                self.get(answer)

    def test_rejected_key(self) -> None:
        with self.assertRaises(sc.AuthError):
            self.get((401, b'{"success": false}'))

    def test_server_trouble_is_an_ordinary_error(self) -> None:
        with self.assertRaises(RuntimeError):
            self.get((503, b""))

    def test_a_result_matching_the_filters_is_used(self) -> None:
        self.assertEqual(sc.check_grid(grid()), "https://cdn.example/cover.png")

    def test_a_result_not_matching_the_filters_is_refused(self) -> None:
        for change in ({"width": 920}, {"height": 430}, {"mime": "image/webp"}, {"nsfw": True}, {"humor": True},
                       {"url": "http://cdn.example/cover.png"}, {"url": "file:///C:/cover.png"}, {"url": None}):
            with self.subTest(change=change), self.assertRaises(sc.ApiError):
                sc.check_grid(grid(**change))


class TestKeyCheck(Case):
    def test_accepted_key(self) -> None:
        self.routes(PROBE)
        self.assertTrue(sc.key_is_valid(KEY))

    def test_rejected_key(self) -> None:
        self.routes({"/grids/steam/70?": (401, b'{"success": false}')})
        self.assertFalse(sc.key_is_valid(KEY))

    def test_stops_when_the_api_seems_to_have_changed(self) -> None:
        for answer in (grids(), grids(grid(width=920)), HTML_404):
            self.routes({"/grids/steam/70?": answer})
            with self.subTest(answer=answer), self.assertRaises(SystemExit) as stop:
                sc.key_is_valid(KEY)
            self.assertIn("API has probably changed", str(stop.exception.code))

    def test_a_cover_not_matching_the_filters_stops_the_run_unwritten(self) -> None:
        grid_dir = self.make_steam({10: app("Ten")})
        self.save_key()
        self.routes({**PROBE, "/grids/steam/10?": grids(grid(nsfw=True, url="https://cdn.example/10.png")),
                     "cdn.example/10.png": (200, PNG)})
        code, out = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("API has probably changed", out)
        self.assertEqual(list(grid_dir.iterdir()), [])

    def test_unexpected_answer_mid_run_stops_new_lookups(self) -> None:
        self.make_steam({10: app("Ten"), 20: app("Twenty"), 30: app("Thirty")})
        self.save_key()

        def slow_not_found() -> Response:
            time.sleep(1)  # if it started before the run stopped, it's still running when 30 is cancelled
            return GAME_NOT_FOUND

        fake = self.routes({**PROBE, "/grids/steam/10?": HTML_404, "/grids/steam/20?": slow_not_found,
                            "/grids/steam/30?": GAME_NOT_FOUND})
        code, out = self.run_main("--workers", "1")
        self.assertEqual(code, 1)
        self.assertIn("API has probably changed", out)
        self.assertIn("Not processed (stopped early):", out)  # 30, and 20 unless it had already started
        self.assertFalse(any("/grids/steam/30?" in url for url in fake.calls))
        self.assertNotIn("10", json.loads(sc.STATE_FILE.read_text(encoding="utf-8"))["not_found"])


# ---------------------------------------------------------------- 2. adopting existing covers

class TestAdopt(Case):
    def test_adopts_only_covers_of_games_with_just_a_placeholder(self) -> None:
        grid_dir = self.make_steam({10: app("Ten"), 20: app("Twenty", art=True)},
                                   {"10p.png": PNG, "20p.png": PNG, "30p.png": PNG, "notes.txt": b"x"})
        written: sc.Written = {}
        self.assertEqual(sc.adopt_covers([grid_dir], written, sc.read_appinfo(self.steam)), [("10", "10p.png")])
        self.assertEqual(written, {"1": {"10": hashlib.sha1(PNG).hexdigest()}})

    def test_dry_run_lists_each_cover_with_its_game(self) -> None:
        self.make_steam({10: app("Ten Game")}, {"10p.png": PNG})
        code, out = self.run_main("--dry-run", "--adopt-existing")
        self.assertEqual(code, 0)
        self.assertIn("10 Ten Game: would adopt 10p.png", out)
        self.assertIn("Would adopt 1 existing cover (", out)
        self.assertFalse(sc.STATE_FILE.exists())


# ---------------------------------------------------------------- 3. state.json

class TestState(Case):
    def load(self, text: str, dry_run: bool = False) -> tuple[sc.State, str]:
        sc.STATE_FILE.write_text(text, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            state = sc.load_state(dry_run)
        return state, out.getvalue()

    def set_aside(self) -> list[Path]:
        return list(self.tmp.glob("state-damaged-*.json"))

    def test_a_missing_file_is_a_first_run(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sc.load_state(False), {"not_found": {}, "written": {}})
        self.assertEqual(out.getvalue(), "")

    def test_invalid_json_is_set_aside(self) -> None:
        state, out = self.load("{not json")
        self.assertEqual(state, {"not_found": {}, "written": {}})
        self.assertFalse(sc.STATE_FILE.exists())
        self.assertEqual(len(self.set_aside()), 1)
        self.assertIn("not valid JSON", out)
        self.assertIn("--adopt-existing", out)

    def test_a_wrong_layout_is_set_aside(self) -> None:
        for text in ("[]", '{"written": []}', '{"not_found": 5}'):
            with self.subTest(text=text):
                _, out = self.load(text)
                self.assertIn("was damaged", out)
                self.assertFalse(sc.STATE_FILE.exists())

    def test_damage_outside_the_cover_records_needs_no_adopting(self) -> None:
        good = "0123456789abcdef0123456789abcdef01234567"
        state, out = self.load(json.dumps({"not_found": 5, "written": {"1": {"10": good}}}))
        self.assertEqual(state["written"], {"1": {"10": good}})
        self.assertIn("was damaged", out)
        self.assertNotIn("--adopt-existing", out)

    def test_a_dry_run_leaves_a_damaged_file_alone(self) -> None:
        _, out = self.load("{not json", dry_run=True)
        self.assertTrue(sc.STATE_FILE.exists())
        self.assertEqual(self.set_aside(), [])
        self.assertIn("A real run would set it aside", out)

    def test_bad_records_are_dropped_and_the_good_ones_kept(self) -> None:
        good = "0123456789abcdef0123456789abcdef01234567"
        state, out = self.load(json.dumps({"written": {"1": {"10": good, "20": "not a sha1"}}}))
        self.assertEqual(state["written"], {"1": {"10": good}})
        self.assertIn("malformed tracked-cover records: 1", out)
        self.assertIn("kept 1 tracked cover.", out)
        self.assertEqual(len(self.set_aside()), 1)

    def test_an_unreadable_file_stops_the_run(self) -> None:
        sc.STATE_FILE.mkdir()
        with self.assertRaises(SystemExit):
            sc.load_state(False)

    def test_bad_or_expired_lookups_are_dropped_quietly(self) -> None:
        now = time.time()
        state, out = self.load(json.dumps({"not_found": {"10": now - 60, "20": "yesterday",
                                                         "30": now - sc.NOT_FOUND_TTL - 60}}))
        self.assertEqual(list(state["not_found"]), ["10"])
        self.assertEqual(out, "")


# ---------------------------------------------------------------- 4. appinfo.vdf

class TestAppinfo(Case):
    def read(self, data: bytes) -> sc.AppInfo:
        appcache = self.tmp / "Steam" / "appcache"
        appcache.mkdir(parents=True, exist_ok=True)
        (appcache / "appinfo.vdf").write_bytes(data)
        return sc.read_appinfo(self.tmp / "Steam")

    def test_reads_kind_art_and_name(self) -> None:
        apps = {10: app("Ten"), 20: app("Twenty Playtest", "Beta", art=True)}
        for version in (28, 29):
            with self.subTest(version=version):
                self.assertEqual(self.read(build_appinfo(apps, version)),
                                 {"10": sc.App("game", False, "Ten"), "20": sc.App("beta", True, "Twenty Playtest")})

    def test_a_zero_filled_entry_is_skipped(self) -> None:
        zeros = bytes(60 + 6000)  # an entry that's all zero bytes, as in a half-written file
        data = build_appinfo({10: app("Ten")}, extra=struct.pack("<II", 11, len(zeros)) + zeros)
        self.assertEqual(self.read(data), {"10": sc.App("game", False, "Ten")})


# ---------------------------------------------------------------- 5. the API key

class TestKeyHandling(Case):
    def test_invisible_characters_are_dropped(self) -> None:
        self.assertEqual(sc.clean_key("\ufeff\u200babc123 \n"), "abc123")

    def test_a_key_that_cannot_be_sent_is_never_sent(self) -> None:
        fake = self.routes({})
        self.assertFalse(sc.key_is_valid("abc\u00e9" + KEY[4:]))
        self.assertEqual(fake.calls, [])

    def test_the_prompt_asks_again_until_a_key_works(self) -> None:
        fake = self.routes(PROBE)
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=["abc\u00e9", f"\u200b{KEY}\n"]), contextlib.redirect_stdout(out):
            self.assertEqual(sc.get_api_key(), KEY)
        self.assertIn("doesn't look like an API key", out.getvalue())
        self.assertEqual(len(fake.calls), 1)  # only the good key was sent
        self.assertEqual(sc.ENV_FILE.read_text(encoding="utf-8"), f"SGDB_API_KEY={KEY}\n")

    def test_without_a_terminal_it_explains_how_to_give_a_key(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as stop:
            sc.get_api_key()
        self.assertIn("SGDB_API_KEY=", str(stop.exception.code))

    def test_ctrl_c_at_the_prompt_is_not_swallowed(self) -> None:
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            sc.get_api_key()

    def test_saving_replaces_an_earlier_key_and_keeps_other_settings(self) -> None:
        sc.ENV_FILE.write_text("OTHER=1\nSGDB_API_KEY = old\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            sc.save_env_key("new")
        self.assertEqual(sc.ENV_FILE.read_text(encoding="utf-8"), "OTHER=1\nSGDB_API_KEY=new\n")


# ---------------------------------------------------------------- 6. which apps are games

class TestAppTypes(Case):
    def test_a_playtest_without_art_is_looked_up(self) -> None:
        self.make_steam({10: app("Ten Playtest", "Beta")})
        self.save_key()
        self.routes({**PROBE, "/grids/steam/10?": grids(grid())})
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("10 Ten Playtest: would apply", out)

    def test_store_types(self) -> None:
        items: list[dict[str, object]] = [
            {"appid": 10, "type": 12, "assets": {"library_capsule": "portrait.png"}},  # playtest
            {"appid": 20, "type": 4, "assets": {"library_capsule": "portrait.png"}},  # DLC
            {"appid": 30, "type": 99, "assets": {}},  # a type code this script doesn't know
            {"appid": 40, "type": 2, "assets": {"library_capsule": "abc/library_capsule.jpg"}},  # mod
        ]
        self.routes({"IStoreBrowseService": (200, json.dumps({"response": {"store_items": items}}).encode())})
        check = sc.store_capsules(["10", "20", "30", "40"])
        self.assertEqual((check.placeholder, check.not_game, check.official), ({"10"}, {"20", "30"}, {"40"}))


# ---------------------------------------------------------------- 7. Ctrl+C

class TestCtrlC(Case):
    def slow(self) -> Response:
        time.sleep(2)
        return 500, b""

    def assert_stops_quickly(self, call: Callable[[], object]) -> None:
        threading.Timer(0.2, _thread.interrupt_main).start()
        start = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            call()
        self.assertLess(time.monotonic() - start, 1.5)

    def test_during_the_store_check(self) -> None:
        self.routes({"IStoreBrowseService": self.slow})
        self.assert_stops_quickly(lambda: sc.store_capsules([str(i) for i in range(800)]))

    def test_during_the_key_check(self) -> None:
        self.routes({"/grids/steam/70?": self.slow})
        self.assert_stops_quickly(lambda: sc.key_is_valid(KEY))

    def test_first_ctrl_c_lets_lookups_in_progress_finish(self) -> None:
        self.make_steam({10: app("Ten"), 20: app("Twenty"), 30: app("Thirty")})
        self.save_key()

        def interrupted_lookup() -> Response:
            _thread.interrupt_main()  # Ctrl+C while this lookup is in progress
            time.sleep(1)
            return grids(grid(url="https://cdn.example/10.png"))

        self.routes({**PROBE, "/grids/steam/10?": interrupted_lookup, "cdn.example/10.png": (200, PNG)})
        code, out = self.run_main("--workers", "1")
        self.assertEqual(code, 1)
        self.assertIn("Interrupted, finishing the downloads already in progress", out)
        self.assertIn("10 Ten: applied", out)
        self.assertIn("Not processed (stopped early): 2", out)

    def test_a_hard_quit_waits_for_writes_in_progress(self) -> None:
        guard = sc.WriteGuard()
        entered = threading.Event()

        def slow_write() -> None:
            with guard.writing():
                entered.set()
                time.sleep(0.3)

        writer = threading.Thread(target=slow_write)
        writer.start()
        entered.wait()
        start = time.monotonic()
        guard.stop_and_wait()
        self.assertGreater(time.monotonic() - start, 0.2)
        writer.join()
        with self.assertRaises(RuntimeError), guard.writing():
            pass  # no new writes once stopping


# ---------------------------------------------------------------- 9-11. output

class TestOutput(Case):
    def test_plural(self) -> None:
        self.assertEqual([sc.plural(n, "app") for n in (0, 1, 2)], ["0 apps", "1 app", "2 apps"])

    def test_header_counts_placeholder_games(self) -> None:
        self.make_steam({10: app("Ten")})
        self.save_key()
        self.routes({**PROBE, "/grids/steam/10?": GAME_NOT_FOUND})
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("1 app to look up on SteamGridDB (1 with only Steam's auto-generated placeholder).", out)
        self.assertIn("Not on SteamGridDB: 1", out)
        self.assertNotIn("included above", out)

    def test_nothing_to_do(self) -> None:
        self.make_steam({10: app("Ten", art=True)})
        code, out = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("No apps need a cover from SteamGridDB.", out)

    def test_verbose_lists_games_skipped_from_an_earlier_run(self) -> None:
        self.make_steam({10: app("Ten Game")})
        sc.STATE_FILE.write_text(json.dumps({"not_found": {"10": time.time() - 2 * 86400 - 60}}), encoding="utf-8")
        code, out = self.run_main("--dry-run", "-v")
        self.assertEqual(code, 0)
        self.assertIn("10 Ten Game: no art on SteamGridDB (checked 2 days ago; --recheck to look again)", out)


# ---------------------------------------------------------------- deciding what each app needs

class TestClassify(unittest.TestCase):
    GRID = Path("grid")
    PLACEHOLDER = sc.App("game", False, "Ten")
    OFFICIAL = sc.App("game", True, "Ten")

    def classify(self, appinfo: sc.AppInfo, *, custom: bool = False, not_found: float | None = None,
                 force: bool = False, explicit: bool = False, recheck: bool = False,
                 retired: Iterable[str] = ()) -> sc.Plan:
        listing = sc.Listing(frozenset({"10p.png"} if custom else set[str]()), ignore_case=True)
        return sc.classify(["10"], [self.GRID], {self.GRID: listing}, appinfo,
                           {} if not_found is None else {"10": not_found}, NOW, force=force, explicit=explicit,
                           recheck=recheck, check_official=not (explicit and force), retired=set(retired))

    def test_custom_cover(self) -> None:
        self.assertEqual(self.classify({"10": self.PLACEHOLDER}, custom=True).custom, ["10"])

    def test_not_a_game(self) -> None:
        self.assertEqual(self.classify({"10": sc.App("dlc", False, "")}).not_game, 1)

    def test_official_art(self) -> None:
        self.assertEqual(self.classify({"10": self.OFFICIAL}).official, {"10"})

    def test_skipped_after_an_earlier_lookup_found_nothing(self) -> None:
        self.assertEqual(self.classify({"10": self.PLACEHOLDER}, not_found=NOW - 60).skipped, {"10": NOW - 60})

    def test_placeholder_is_looked_up(self) -> None:
        plan = self.classify({"10": self.PLACEHOLDER})
        self.assertEqual((plan.todo, plan.placeholders), ({"10": [self.GRID]}, 1))

    def test_unknown_to_appinfo_goes_to_the_store(self) -> None:
        self.assertEqual(self.classify({}).undecided, {"10": [self.GRID]})

    def test_just_retired(self) -> None:
        self.assertEqual(self.classify({"10": self.PLACEHOLDER}, retired=["10"]), sc.Plan())

    def test_explicit_appid_is_looked_up_even_if_not_a_game_or_found_nothing_before(self) -> None:
        self.assertIn("10", self.classify({"10": sc.App("dlc", False, "")}, explicit=True).todo)
        self.assertIn("10", self.classify({"10": self.PLACEHOLDER}, explicit=True, not_found=NOW - 60).todo)

    def test_recheck_ignores_earlier_lookups(self) -> None:
        self.assertIn("10", self.classify({"10": self.PLACEHOLDER}, recheck=True, not_found=NOW - 60).todo)

    def test_force_replaces_custom_covers_but_not_official_art(self) -> None:
        self.assertIn("10", self.classify({"10": self.PLACEHOLDER}, custom=True, force=True).todo)
        self.assertEqual(self.classify({"10": self.OFFICIAL}, force=True).official, {"10"})

    def test_explicit_force_overrides_official_art(self) -> None:
        plan = self.classify({"10": self.OFFICIAL}, explicit=True, force=True)
        self.assertEqual((plan.todo, plan.placeholders), ({"10": [self.GRID]}, 0))
        self.assertIn("10", self.classify({}, explicit=True, force=True).todo)


# ---------------------------------------------------------------- extras

class TestRunLock(Case):
    def test_only_one_holder_at_a_time(self) -> None:
        path = self.tmp / "test.lock"
        first = sc.lock_file(path)
        assert first is not None
        self.assertIsNone(sc.lock_file(path))
        sc.unlock_file(first)
        again = sc.lock_file(path)
        assert again is not None
        sc.unlock_file(again)

    def test_a_real_run_stops_while_another_holds_the_lock(self) -> None:
        self.make_steam({10: app("Ten", art=True)})
        held = sc.lock_file(sc.LOCK_FILE)
        assert held is not None
        self.addCleanup(sc.unlock_file, held)
        with self.assertRaises(SystemExit) as stop:
            self.run_main()
        self.assertIn("already in progress", str(stop.exception.code))


class TestRedirects(unittest.TestCase):
    def follow(self, url: str, new_url: str, method: str = "GET") -> urllib.request.Request:
        request = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {KEY}"})
        new = sc.RedirectHandler().redirect_request(request, io.BytesIO(), 302, "Found", HTTPMessage(), new_url)
        assert new is not None
        return new

    def test_the_key_only_follows_redirects_within_the_same_secure_site(self) -> None:
        here = "https://www.steamgriddb.com/api/v2/a"
        self.assertTrue(self.follow(here, "https://www.steamgriddb.com/api/v2/b").has_header("Authorization"))
        self.assertFalse(self.follow(here, "https://elsewhere.example/b").has_header("Authorization"))
        self.assertFalse(self.follow(here, "http://www.steamgriddb.com/api/v2/b").has_header("Authorization"))

    def test_a_head_request_stays_a_head(self) -> None:
        self.assertEqual(self.follow("https://a.example/x", "https://a.example/y", "HEAD").get_method(), "HEAD")


class TestCertificateErrors(unittest.TestCase):
    def test_are_not_retried(self) -> None:
        error = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with mock.patch.object(sc.OPENER, "open", side_effect=error), mock.patch.object(time, "sleep") as sleep, \
                self.assertRaises(RuntimeError) as failure:
            REAL_HTTP("https://www.steamgriddb.com/api/v2/x")
        self.assertIn("certificate check", str(failure.exception))
        sleep.assert_not_called()


class TestSteamFolders(Case):
    def test_finds_steam_on_linux_and_macos(self) -> None:
        for platform, folder in (("linux", Path(".local", "share", "Steam")),
                                 ("darwin", Path("Library", "Application Support", "Steam"))):
            with self.subTest(platform=platform):
                home = self.tmp / platform
                (home / folder / "appcache").mkdir(parents=True)
                with mock.patch.object(sys, "platform", platform), mock.patch.object(Path, "home", return_value=home):
                    self.assertEqual(sc.find_steam(None), home / folder)

    def test_the_listing_follows_the_folders_case_rules(self) -> None:
        (self.tmp / "10P.PNG").write_bytes(PNG)
        listing = sc.list_folder(self.tmp)
        folder_ignores_case = (self.tmp / "10p.png").exists()  # True on Windows and macOS, False on Linux
        self.assertEqual(listing.ignore_case, folder_ignores_case)
        self.assertEqual("10p.png" in listing, folder_ignores_case)

    def test_case_sensitive_folders_match_names_exactly(self) -> None:
        (self.tmp / "10P.PNG").write_bytes(PNG)
        with mock.patch.object(os.path, "samefile", side_effect=FileNotFoundError):
            self.assertFalse(sc.ignores_case(self.tmp, ["10P.PNG"]))  # no file under the other spelling
        with mock.patch.object(sc, "ignores_case", return_value=False):  # as on Linux
            listing = sc.list_folder(self.tmp)
        self.assertNotIn("10p.png", listing)  # Steam on Linux wouldn't use 10P.PNG either
        self.assertEqual(sc.existing_custom(self.tmp, "10", listing), [])


# ---------------------------------------------------------------- version

class TestVersion(unittest.TestCase):
    def test_version_flag(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            sc.main(["--version"])
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(out.getvalue(), f"sgdb-covers {sc.__version__}\n")

    def test_the_version_has_the_form_release_tags_and_the_exe_use(self) -> None:
        self.assertRegex(sc.__version__, r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")  # see sgdb-covers.spec
        self.assertEqual(sc.USER_AGENT, f"sgdb-covers/{sc.__version__}")


# ---------------------------------------------------------------- running as an .exe

class TestAsExe(unittest.TestCase):
    def test_files_live_next_to_the_exe(self) -> None:
        exe = Path(tempfile.gettempdir(), "tools", "sgdb-covers.exe")
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(exe)):
            self.assertEqual(sc.app_folder(), exe.resolve().parent)
        self.assertEqual(sc.app_folder(), Path(sc.__file__).resolve().parent)  # the script: next to it

    def owns_window(self, *, frozen: bool = True, tty: bool = True, unpacked: str = "_MEI12345",
                    processes: int = 2) -> bool:
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        with mock.patch.object(sys, "frozen", frozen, create=True),                 mock.patch.object(sys, "_MEIPASS", str(Path(tempfile.gettempdir(), unpacked)), create=True),                 mock.patch.object(sys, "stdin", stdin), mock.patch.object(sc, "console_process_count", return_value=processes):
            return sc.owns_console_window()

    def test_a_double_clicked_exe_has_its_own_window(self) -> None:
        self.assertTrue(self.owns_window(processes=2))  # one-file build: PyInstaller's launcher and the program
        self.assertTrue(self.owns_window(unpacked="_internal", processes=1))  # one-folder build

    def test_otherwise_the_window_isnt_its_own(self) -> None:
        self.assertFalse(self.owns_window(processes=3))  # a terminal shares it
        self.assertFalse(self.owns_window(unpacked="_internal", processes=2))
        self.assertFalse(self.owns_window(processes=0))  # no console at all
        self.assertFalse(self.owns_window(frozen=False))  # the .py script
        self.assertFalse(self.owns_window(tty=False))  # input redirected: nobody there to press Enter

    def run_cli(self, main: Callable[[], int], *, own_window: bool,
                pause: mock.MagicMock) -> tuple[int | str | None, str]:
        err = io.StringIO()
        with mock.patch.object(sc, "main", side_effect=main),                 mock.patch.object(sc, "owns_console_window", return_value=own_window),                 mock.patch("builtins.input", pause), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            sc.cli()
        return stop.exception.code, err.getvalue()

    def test_waits_for_enter_only_in_its_own_window(self) -> None:
        pause = mock.MagicMock()
        self.assertEqual(self.run_cli(lambda: 0, own_window=True, pause=pause), (0, ""))
        pause.assert_called_once()
        pause = mock.MagicMock()
        self.assertEqual(self.run_cli(lambda: 1, own_window=False, pause=pause), (1, ""))
        pause.assert_not_called()

    def test_an_error_is_shown_before_waiting(self) -> None:
        def fail() -> int:
            sys.exit("Steam library cache not found")

        pause = mock.MagicMock(side_effect=KeyboardInterrupt)  # Ctrl+C instead of Enter: still exits
        code, err = self.run_cli(fail, own_window=True, pause=pause)
        self.assertEqual(code, 1)
        self.assertIn("Steam library cache not found", err)
        pause.assert_called_once()


# ---------------------------------------------------------------- writing and removing covers

class TestCovers(Case):
    def test_a_real_run_writes_and_records_the_cover(self) -> None:
        grid_dir = self.make_steam({10: app("Ten Game")})
        self.save_key()
        self.routes({**PROBE, "/grids/steam/10?": grids(grid(url="https://cdn.example/10.png")),
                     "cdn.example/10.png": (200, PNG)})
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("[1/1] 10 Ten Game: applied https://cdn.example/10.png", out)
        self.assertEqual((grid_dir / "10p.png").read_bytes(), PNG)
        state = json.loads(sc.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["written"], {"1": {"10": hashlib.sha1(PNG).hexdigest()}})

    def test_only_unchanged_covers_of_games_with_art_are_removed(self) -> None:
        grid_dir = self.make_steam({10: app("Ten", art=True), 20: app("Twenty", art=True), 30: app("Thirty")},
                                   {"10p.png": PNG, "20p.png": JPEG, "30p.png": PNG})
        ours = hashlib.sha1(PNG).hexdigest()
        written: sc.Written = {"1": {"10": ours, "20": ours, "30": ours}}
        retired = sc.retire_covers([grid_dir], written, sc.read_appinfo(self.steam), dry_run=False)
        self.assertEqual(retired, [("10", "10p.png")])
        self.assertFalse((grid_dir / "10p.png").exists())
        self.assertTrue((grid_dir / "20p.png").exists())  # swapped by the user: left alone, no longer tracked
        self.assertEqual(written, {"1": {"30": ours}})  # still no official art: still tracked

    def test_a_failed_write_restores_the_old_cover(self) -> None:
        grid_dir = self.tmp / "grid"
        grid_dir.mkdir()
        (grid_dir / "10p.jpg").write_bytes(JPEG)
        with mock.patch.object(sc, "atomic_write", side_effect=OSError("disk full")), self.assertRaises(RuntimeError):
            sc.write_cover(grid_dir, "10", ".png", PNG)
        self.assertEqual([p.name for p in grid_dir.iterdir()], ["10p.jpg"])
        self.assertEqual((grid_dir / "10p.jpg").read_bytes(), JPEG)


if __name__ == "__main__":
    unittest.main()
