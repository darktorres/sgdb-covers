# sgdb-covers

Fills in the vertical (600x900) Steam library covers that are missing, using the top-voted art on [SteamGridDB](https://www.steamgriddb.com).

## How it works

1. **Find the games.** It lists the games in Steam's library cache (`appcache\librarycache\<appid>\`).
2. **Skip what's already done.** It skips games that already have a custom cover in `userdata\<id>\config\grid\<appid>p.png/jpg`.
3. **Skip games with an official cover.** It reads Steam's local app database (`appcache\appinfo.vdf`), which records whether each app has real vertical art, and skips those. Games that only have Steam's auto-generated placeholder (the wide header pasted on a blurred background) count as missing a cover, even though the placeholder sits in Steam's cache. DLC, tools and soundtracks are skipped. For apps the database doesn't list, it asks the Steam store instead.
4. **Download the rest.** For every other game, it downloads the highest-scored static 600x900 grid from SteamGridDB to `grid\<appid>p.png` (or `.jpg`).

Games with no art on SteamGridDB are recorded in `state.json` and skipped for 30 days. Use `--recheck` to look them up again anyway.

**When a game gets official art later**, the cover this tool downloaded would keep hiding it, because Steam always prefers custom covers. So the tool records each cover it writes (with a hash of the file) in `state.json`. Each run, it removes recorded covers whose game now has real official art, so the official cover shows. Covers you replaced or added yourself are never removed: if the file isn't exactly the one the tool wrote, it's left alone.

Covers written before this tracking existed can be adopted once with `--adopt-existing`. Only covers for games that currently have just Steam's placeholder are adopted. A custom cover for a game that already has official art is a deliberate choice, so it's never adopted. Covers you set through Steam's own "Set Custom Artwork" use the same file names and can't be told apart, so check the list from `--adopt-existing --dry-run` first. A cover forced over official art with `--appid … --force` is never tracked, so it's never removed.

The script only adds or replaces `<appid>p.*` files. Any other files in the grid folder are left alone, and if the folder is a symlink (Dropbox, for example), files are written through the link.

## Usage

Requires Python 3.9+ and nothing else.

```
python sgdb_covers.py
```

The first time you run it, the script prints the [API key page](https://www.steamgriddb.com/profile/preferences/api) and asks you to paste your key. The key is saved to `.env` next to the script.

| Flag | |
|---|---|
| `--dry-run` | List what would be downloaded without writing anything |
| `--force` | Also replace covers you've already customized. Combined with `--appid`, it also replaces official Steam covers |
| `--recheck` | Look up games again even if they had no art on an earlier run |
| `--appid 123 456` | Only process these games |
| `--user <id>` | Only this `userdata` account (creates `config\grid` if needed) |
| `--steam-path <dir>` | Steam folder, if it can't be found in the registry |
| `--workers N` | Number of parallel requests (default 6) |
| `--adopt-existing` | Start tracking existing covers for games without official art, so they're removed once official art appears (run once) |
| `-v` | Also print skipped and not-found games |

Output is colored in a terminal. Set `NO_COLOR=1` to turn colors off; they're also off automatically when output is redirected to a file.

Restart Steam afterwards to see the new covers.
