# minigit

[English](README.md) | [日本語](README.ja.md) | [中文](README.zh.md)

A minimal Git hosting server for closed networks. It runs on the Python standard
library and the git CLI, with no third-party packages and no database server.

![Home](docs/screenshots/home_en.png)

## Features

- Clone and push over HTTP, with per-user accounts (SQLite, PBKDF2-SHA256 passwords)
- Issues and Pull Requests. Split or unified diffs, word-level highlighting, and
  conflict resolution in the browser (git 2.38+)
- Branch create/delete, protected branches, file editing with commit
- Syntax highlighting for py/java/js/c/sh/sql/html/css/ini, written from scratch, no JS
- Light and dark themes
- UI in English, 日本語, and 中文. See [Languages](#languages)
- Single-file self-extracting distribution (`pack.py` + `split.sh`). See
  [Bring it offline](#bring-it-offline)

## Requirements

- Python 3.8 or newer
- git 2.x (conflict resolution in the browser needs git 2.38 or newer)

## Start it

```sh
./run.sh            # http://localhost:8080/
./run.sh 9000       # other port
```

On first start, `run.sh` creates the `admin` user with a random password and
prints it to the console. Repositories and the user database live in `./repos`.

Environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `MINIGIT_ROOT` | Where repositories and the database live | `./repos` |
| `MINIGIT_USERS` | Users imported on first start, `alice:pw1,bob:pw2` (first is admin) | none |
| `MINIGIT_GIT` | Path to the git binary | auto-detected |
| `MINIGIT_HOST` | Bind address | all IPv4 and IPv6 |
| `MINIGIT_LANG` | Fallback UI language (`en`, `ja`, `zh`) | `en` |
| `MINIGIT_DEBUG` | Set to `1` to print every git command | off |

Forgot a password: `python3 minigit.py passwd USER NEWPASSWORD`

## Use with git

```sh
git clone http://USERNAME:PASSWORD@localhost:8080/REPO.git
```

## Languages

The UI ships in English, Japanese, and Simplified Chinese. The text lives in
`minigit_en.json`, `minigit_ja.json`, and `minigit_zh.json` next to
`minigit.py`. The server picks a language in this order:

1. `?lang=en|ja|zh` in the URL (also stored in a cookie)
2. The `mg_lang` cookie from an earlier visit
3. Your browser's `Accept-Language` header
4. `MINIGIT_LANG`, or `en`

Every page has a language switcher in the header. Missing keys fall back to
English, then to Japanese. To add a language, copy one JSON file, translate the
values, and restart. Keys are `{...}`-free plain text, so you can translate the
JSON without touching code.

`extract.py` is the codemod that cut the strings out of `minigit.py` into the
JSON files. It is a development tool, not part of the server.

![File view](docs/screenshots/file_en.png)

## Bring it offline

You can transfer the server to an air-gapped Windows machine that has only
Python and cmd.exe (no PowerShell). `pack.py` bundles `minigit.py` and the three
language files into one ASCII-only, self-extracting `minigit_packed.py`.
`split.sh` cuts it into parts under 20 KB each.

```
[Mac/Linux]  python3 pack.py  ->  ./split.sh minigit_packed.py 19
[transfer]   minigit_packed.py.part.*, minigit_packed.py.sha256, join.cmd, unpack.py
[Windows]    join.cmd minigit_packed.py  ->  python minigit_packed.py 8080
                                        ->  python unpack.py   (restore all sources)
```

| File | Purpose |
|---|---|
| `minigit.py` | The server |
| `minigit_<lang>.json` | UI strings per language |
| `run.sh` | Start script for Mac / Linux |
| `pack.py` / `unpack.py` | Create the self-extracting bundle (bz2 + base85) / restore all sources with SHA256 checks |
| `split.sh` / `join.sh` | Split and join parts (Mac / Linux) |
| `split.cmd` / `join.cmd` | Split and join parts (Windows cmd only) |

## Layout

Everything the server needs is `minigit.py` and the three JSON files.
`schema.sql` is only a demo fixture used in test repositories.

## License

MIT
