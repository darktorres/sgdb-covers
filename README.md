# sgdb-covers

Fills in the vertical (600x900) Steam library covers that are missing, using the top-voted art on [SteamGridDB](https://www.steamgriddb.com).

## How it works

1. **Find the games.** It lists the games in Steam's library cache (`appcache/librarycache/<appid>/`).
2. **Skip what's already done.** It skips games that already have a custom cover in `userdata/<id>/config/grid/<appid>p.png/jpg`.
3. **Skip games with an official cover.** It reads Steam's local app database (`appcache/appinfo.vdf`), which records whether each app has real vertical art, and skips those. Games that only have Steam's auto-generated placeholder (the wide header pasted on a blurred background) count as missing a cover, even though the placeholder sits in Steam's cache. DLC, tools and soundtracks are skipped; playtests and mods count as games. For apps the database doesn't list, it asks the Steam store instead.
4. **Download the rest.** For every other game, it downloads SteamGridDB's top-voted static 600x900 cover to `grid/<appid>p.png` (or `.jpg`). Each answer is checked against what was asked for (size, file type, no NSFW or humor art). If SteamGridDB answers in a way its API doesn't, as it would after an API change, the run stops before marking any game as having no art.

Games with no art on SteamGridDB are recorded in `state.json` and skipped for 30 days. Use `--recheck` to look them up again anyway.

**When a game gets official art later**, the cover this tool downloaded would keep hiding it, because Steam always prefers custom covers. So the tool records each cover it writes (with a hash of the file) in `state.json`. Each run, it removes recorded covers whose game now has real official art, so the official cover shows. Covers you replaced or added yourself are never removed: if the file isn't exactly the one the tool wrote, it's left alone.

Covers written before this tracking existed can be adopted once with `--adopt-existing`. Only covers for games that currently have just Steam's placeholder are adopted. A custom cover for a game that already has official art is a deliberate choice, so it's never adopted. Covers you set through Steam's own "Set Custom Artwork" use the same file names and can't be told apart, so check the list (each cover with its game's name) from `--adopt-existing --dry-run` first. A cover forced over official art with `--appid … --force` is never tracked, so it's never removed.

If `state.json` gets damaged, what's still valid is kept, the original is set aside as `state-damaged-<date>.json`, and a warning says what was lost. Only one run works at a time: a second one stops with a message instead of mixing its changes with the first one's.

The script only touches `<appid>p.*` files: it adds and replaces them, and removes the ones it wrote once official art appears. While writing, it briefly creates hidden `.<name>.tmp` and `.<name>.bak` files next to them. Any other files in the grid folder are left alone, and if the folder is a symlink (Dropbox, for example), files are written through the link.

## Usage

On Windows, the simplest way is `sgdb-covers.exe`, which needs nothing installed: download it from this repository's Releases page, put it in any folder and run it. It keeps its `.env` and `state.json` next to itself. When it's double-clicked, it waits for Enter before closing its window, so the summary can be read. Windows may warn that the publisher is unknown, because the .exe isn't code-signed.

The script itself runs anywhere with Python 3.11+ and nothing else:

```
python sgdb_covers.py
```

Both take the same flags (`sgdb-covers.exe --dry-run`, `python sgdb_covers.py --dry-run`).

The first time there are covers to download, it prints the [API key page](https://www.steamgriddb.com/profile/preferences/api) and asks you to paste your key. The key is saved to `.env` next to the script (or the .exe).

| Flag | |
|---|---|
| `--dry-run` | List what would be downloaded without writing anything |
| `--force` | Also replace covers you've already customized. Combined with `--appid`, it also replaces official Steam covers |
| `--recheck` | Look up games again even if they had no art on an earlier run |
| `--appid 123 456` | Only process these games |
| `--user <id>` | Only this `userdata` account (creates `config/grid` if needed) |
| `--steam-path <dir>` | Steam folder, if it isn't found automatically |
| `--workers N` | Number of parallel requests (default 6) |
| `--adopt-existing` | Start tracking existing covers for games without official art, so they're removed once official art appears (run once; add `--dry-run` to see the list first) |
| `-v` | Also list each game left without a cover and why (no art on SteamGridDB now or on an earlier run, or the Steam store check failed), games found to have official art during the check, and each adopted cover |
| `--version` | Print the version |

The Steam folder is found automatically: from the registry on Windows, and in the usual places on Linux (`~/.steam/steam`, `~/.local/share/Steam`, Flatpak, Snap) and macOS (`~/Library/Application Support/Steam`). It's been used against a real Steam library on Windows; on Linux and macOS, only the automated tests have run it.

Output is colored in a terminal. Set `NO_COLOR=1` to turn colors off; they're also off automatically when output is redirected to a file.

The exit code is 0 when everything went through, 1 when some games failed or the run stopped early, and 130 after quitting with Ctrl+C.

Restart Steam afterwards to see the new covers.

## Development

The tests use only the standard library and need no network:

```
python -m unittest
```

To also run them on Python 3.11, the oldest supported version: `uv run --no-project --python 3.11 python -m unittest`.

`pyproject.toml` holds the strict settings for `mypy` and `pyright`; run either from this folder. The exact versions CI uses are pinned, with hashes, in `requirements/dev.txt`:

```
pip install --require-hashes -r requirements/dev.txt
```

To build the .exe (on Windows):

```
pip install --require-hashes -r requirements/build.txt
pyinstaller --clean --noconfirm sgdb-covers.spec
```

It lands in `dist\`. To test it, point `SGDB_COVERS_EXE` at it and run `python -m unittest tests.test_exe`. These tests are skipped when the variable isn't set.

The `.in` files in `requirements/` list the tools; the pinned `.txt` files are generated from them with the `uv` command at the top of each `.in` file.

### CI

`.github/workflows/ci.yml` runs on every push and pull request:
- the tests on Windows, Linux and macOS, with Python 3.11 and 3.15;
- the type checkers;
- then a build of the .exe, with Python 3.14 and the pinned PyInstaller.

The .exe is tested and then attached to the run, where it can be downloaded.

To publish a release:
1. Set `__version__` at the top of `sgdb_covers.py`, then commit and push. That one value is what `--version`, the User-Agent and the .exe's Properties > Details all show.
2. Write the release notes (Markdown) to a file, say `notes.md`.
3. Create an annotated tag whose message is those notes, and push it:

```
git tag -a v1.0.0 --cleanup=verbatim -F notes.md
git push origin v1.0.0
```

`--cleanup=verbatim` keeps Markdown headings, which git would otherwise strip as comments. CI only publishes a tag that matches `__version__`: `v1.0.0` for `"1.0.0"`.

The release holds the .exe, its SHA-256 checksum and `LICENSES.txt`. That file is written by `tools/licenses.py` from the exact Python and PyInstaller the .exe was built with, and covers everything in the .exe. For a public repository the release also gets a signed build provenance, which proves the .exe was built by this workflow from that commit: `gh attestation verify sgdb-covers.exe --repo <owner>/<repo>`. A tag with a hyphen, such as `v1.1.0-rc1`, is marked as a pre-release.

Dependabot (`.github/dependabot.yml`) keeps the pinned actions and tools up to date with weekly pull requests, which CI tests like any other change.

## License

MIT: see [LICENSE](LICENSE). The .exe also contains the Python runtime, with the libraries Python includes (OpenSSL among them), and PyInstaller's bootloader. They come under their own licenses, all collected in `LICENSES.txt` next to the .exe on each release.
