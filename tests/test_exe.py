"""Tests for a built sgdb-covers.exe: set SGDB_COVERS_EXE to its path (CI does, after building it).
Skipped otherwise; the script itself is covered by test_sgdb_covers.py."""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sgdb_covers as sc
from tests.steam_fixture import app, make_steam

EXE = os.environ.get("SGDB_COVERS_EXE", "")


def version_resource(exe: Path, field: str) -> str:
    """A string from the .exe's version resource, which its Properties > Details tab shows."""
    if sys.platform != "win32":
        raise unittest.SkipTest("version resources are a Windows thing")
    import ctypes
    from ctypes import wintypes
    api = ctypes.windll.version
    size = api.GetFileVersionInfoSizeW(str(exe), None)
    data = ctypes.create_string_buffer(size)
    if not size or not api.GetFileVersionInfoW(str(exe), 0, size, data):
        raise AssertionError("the .exe has no version resource")
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    translation = r"\VarFileInfo\Translation"
    if not api.VerQueryValueW(data, translation, ctypes.byref(pointer), ctypes.byref(length)) or pointer.value is None:
        raise AssertionError("the .exe's version resource has no language")
    language, codepage = struct.unpack("<HH", ctypes.string_at(pointer.value, 4))
    query = rf"\StringFileInfo\{language:04x}{codepage:04x}\{field}"
    text, text_length = ctypes.c_void_p(), wintypes.UINT()
    if not api.VerQueryValueW(data, query, ctypes.byref(text), ctypes.byref(text_length)) or text.value is None:
        raise AssertionError(f"the .exe's version resource has no {field}")
    return ctypes.wstring_at(text.value, text_length.value - 1)  # the length counts the closing NUL


@unittest.skipUnless(EXE, "set SGDB_COVERS_EXE to a built sgdb-covers.exe to test it")
class TestExe(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        # A copy of the .exe, so the files it keeps next to itself land in this test's folder.
        self.exe = self.tmp / "app" / Path(EXE).name
        self.exe.parent.mkdir()
        shutil.copy2(EXE, self.exe)

    def run_exe(self, *args: str) -> subprocess.CompletedProcess[str]:
        # No terminal for input, like CI: the .exe must never wait for Enter then.
        return subprocess.run([str(self.exe), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=120, env={**os.environ, "NO_COLOR": "1"})

    def test_help(self) -> None:
        result = self.run_exe("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SteamGridDB", result.stdout)

    def test_version(self) -> None:
        result = self.run_exe("--version")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, f"sgdb-covers {sc.__version__}"))
        for field in ("FileVersion", "ProductVersion"):
            self.assertEqual(version_resource(self.exe, field), sc.__version__)
        license_text = (Path(__file__).resolve().parent.parent / "LICENSE").read_text(encoding="utf-8")
        self.assertIn(version_resource(self.exe, "LegalCopyright") + "\n", license_text)

    def test_a_run_keeps_its_files_next_to_the_exe(self) -> None:
        steam = self.tmp / "Steam"
        make_steam(steam, {10: app("Ten", art=True)})  # nothing to download, so no network is needed
        result = self.run_exe("--steam-path", str(steam))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No apps need a cover from SteamGridDB.", result.stdout)
        self.assertTrue((self.exe.parent / "state.json").is_file())
        self.assertTrue((self.exe.parent / "sgdb-covers.lock").is_file())

    def test_an_error_exits_with_its_message(self) -> None:
        result = self.run_exe("--steam-path", str(self.tmp / "no Steam here"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Steam library cache not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
