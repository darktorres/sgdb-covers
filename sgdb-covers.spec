# PyInstaller recipe for the standalone sgdb-covers.exe, used by CI and for local builds:
#   pip install --require-hashes -r requirements/build.txt
#   pyinstaller --clean --noconfirm sgdb-covers.spec
# One file with a console window, since it's a command-line tool. UPX compression stays off: it
# saves little and makes antivirus false alarms more likely.

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)

# The version shown under the .exe's Properties > Details comes from the script, so they can't differ.
script = Path(SPECPATH, "sgdb_covers.py").read_text(encoding="utf-8")  # noqa: F821 (PyInstaller provides it)
found = re.search(r'^__version__ = "((\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.]+)?)"$', script, re.MULTILINE)
if found is None:
    raise SystemExit('sgdb_covers.py needs a __version__ like "1.2.3" or "1.2.3-rc1"')
version = found.group(1)
numbers = (int(found.group(2)), int(found.group(3)), int(found.group(4)), 0)
VS_FF_PRERELEASE = 0x2
# The Copyright line under Properties > Details comes from LICENSE.
license_text = Path(SPECPATH, "LICENSE").read_text(encoding="utf-8")  # noqa: F821
copyright_line = re.search(r"^Copyright \(c\) \d{4} \S.*$", license_text, re.MULTILINE)
if copyright_line is None:
    raise SystemExit('LICENSE needs a "Copyright (c) <year> <name>" line')

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, flags=VS_FF_PRERELEASE if found.group(5) else 0),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [  # US English, Unicode
                StringStruct("FileDescription", "sgdb-covers"),  # also the name Task Manager shows
                StringStruct("FileVersion", version),
                StringStruct("InternalName", "sgdb-covers"),
                StringStruct("LegalCopyright", copyright_line.group(0)),
                StringStruct("OriginalFilename", "sgdb-covers.exe"),
                StringStruct("ProductName", "sgdb-covers"),
                StringStruct("ProductVersion", version),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(["sgdb_covers.py"])  # noqa: F821
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="sgdb-covers",
    console=True,
    upx=False,
    version=version_info,
)
