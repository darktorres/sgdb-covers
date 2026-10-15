"""Write LICENSES.txt for sgdb-covers.exe: its own license, then the licenses of everything PyInstaller
bundles into it (the Python runtime with the libraries Python includes, and PyInstaller's bootloader).

Run it with the Python that builds the .exe, after installing requirements/build.txt:
    python tools/licenses.py dist/LICENSES.txt
"""

import importlib.metadata
import platform
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Python's LICENSE.txt leaves out most of the libraries it includes (OpenSSL, expat, zlib, libmpdec,
# mimalloc...); its documentation's license page lists them all.
LICENSE_DOC = "https://raw.githubusercontent.com/python/cpython/v{}/Doc/license.rst"
INCORPORATED = "Licenses and Acknowledgements for Incorporated Software"


def section(title: str, text: str) -> str:
    rule = "=" * 78
    return f"{rule}\n{title}\n{rule}\n\n{text.strip()}\n"


def incorporated_software(license_rst: str) -> str:
    """The part of CPython's Doc/license.rst about the libraries Python includes."""
    start = license_rst.find(INCORPORATED)
    if start < 0:
        raise ValueError(f"Doc/license.rst has no {INCORPORATED!r} section")
    return license_rst[start:]


def build(*, own_license: str, python: str, openssl: str, python_license: str, license_rst: str,
          pyinstaller: str, pyinstaller_license: str) -> str:
    return "\n".join([
        f"sgdb-covers.exe is distributed under the MIT License below. It also contains the Python {python} "
        f"runtime and the libraries Python includes ({openssl} among them), bundled by PyInstaller "
        f"{pyinstaller}. Their licenses follow.\n",
        section("sgdb-covers", own_license),
        section(f"Python {python} (its LICENSE.txt)", python_license),
        section(f"Libraries included in Python {python} (its Doc/license.rst)", incorporated_software(license_rst)),
        section(f"PyInstaller {pyinstaller} (its bootloader is in the .exe)", pyinstaller_license),
    ])


def fetch(url: str) -> str:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                text: str = response.read().decode("utf-8")
                return text
        except OSError:
            if attempt == 2:
                raise
            time.sleep(5)
    raise AssertionError("unreachable")


def pyinstaller_copying() -> str:
    for file in importlib.metadata.distribution("pyinstaller").files or []:
        if file.name == "COPYING.txt":
            return file.read_text(encoding="utf-8")
    raise FileNotFoundError("PyInstaller's COPYING.txt isn't installed")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: python {sys.argv[0]} <output file>")
    python = platform.python_version()
    text = build(
        own_license=(ROOT / "LICENSE").read_text(encoding="utf-8"),
        python=python,
        openssl=ssl.OPENSSL_VERSION,
        python_license=Path(sys.base_prefix, "LICENSE.txt").read_text(encoding="utf-8"),
        license_rst=fetch(LICENSE_DOC.format(python)),
        pyinstaller=importlib.metadata.version("pyinstaller"),
        pyinstaller_license=pyinstaller_copying(),
    )
    Path(sys.argv[1]).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
