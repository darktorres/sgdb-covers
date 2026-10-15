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

Requires Python 3.11+ and nothing else.

```
python sgdb_covers.py
```

The first time there are covers to download, the script prints the [API key page](https://www.steamgriddb.com/profile/preferences/api) and asks you to paste your key. The key is saved to `.env` next to the script.

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

The Steam folder is found automatically: from the registry on Windows, and in the usual places on Linux (`~/.steam/steam`, `~/.local/share/Steam`, Flatpak, Snap) and macOS (`~/Library/Application Support/Steam`). The script is tested on Windows.

Output is colored in a terminal. Set `NO_COLOR=1` to turn colors off; they're also off automatically when output is redirected to a file.

The exit code is 0 when everything went through, 1 when some games failed or the run stopped early, and 130 after quitting with Ctrl+C.

Restart Steam afterwards to see the new covers.

## Development

The tests use only the standard library and need no network:

```
python -m unittest
```

`pyproject.toml` holds the strict settings for `mypy` and `pyright`; run either from this folder. To also run the tests on Python 3.11, the oldest supported version: `uv run --no-project --python 3.11 python -m unittest`.
