# PyInstaller recipe for the standalone sgdb-covers.exe, used by CI and for local builds:
#   pip install --require-hashes -r requirements/build.txt
#   pyinstaller --clean --noconfirm sgdb-covers.spec
# One file with a console window, since it's a command-line tool. UPX compression stays off: it
# saves little and makes antivirus false alarms more likely.

a = Analysis(["sgdb_covers.py"])  # noqa: F821 (PyInstaller provides these names)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="sgdb-covers",
    console=True,
    upx=False,
)
