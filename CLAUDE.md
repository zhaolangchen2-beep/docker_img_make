# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small host-side tool that produces a slim OS image tarball bundling a
specific CPython and GCC, both built from source for the host's CPU
architecture. Output filename:

```
<os_slug>-<py_slug>-<gcc_slug>-<arch>.tar.gz
# e.g. oe2403sp1-py3143-gcc14-aarch64.tar.gz
```

The tarball is a flattened container fs from `docker export`; load it with
`docker import`.

## Layout

- `build.py` — single-script entrypoint. Reads TOML config, renders a
  Dockerfile, runs `docker build`, then `docker create` + `docker export | gzip`.
  Pure stdlib, requires Python 3.11+ for `tomllib`.
- `config.example.toml` — reference config (openEuler 24.03 SP1 + CPython 3.14.3
  + GCC 14.2.0).
- `README.md` — user-facing usage.

There is no separate Dockerfile on disk — `build.py::render_dockerfile`
generates it per build into `./out/.build-<name>/Dockerfile`.

## Commands

```bash
# build (writes ./out/<name>.tar.gz)
./build.py --config config.toml

# inspect rendered Dockerfile only
./build.py --config config.toml --print-dockerfile

# keep the intermediate docker image after exporting
./build.py --config config.toml --keep-image
```

There are no tests or linters wired up.

## Architecture notes for future edits

- **Single-arch, host-native.** `host_arch()` reads `platform.machine()`. Do
  not introduce QEMU/buildx multi-arch unless the requirement changes — the
  output filename and slimming logic both assume native build.
- **Slimming depends on single-RUN stages.** Each phase (deps / CPython / GCC /
  cleanup) is one `RUN`. `docker export` flattens layers anyway, but keeping
  source removal inside the same RUN as the build also keeps the intermediate
  image small. Don't split a build stage across multiple RUNs without a
  matching cleanup pass.
- **Package-manager dispatch** lives in three module-level dicts: `DEFAULT_DEPS`,
  `PKG_INSTALL`, `PKG_CACHE_CLEAN`, keyed by `base.pkg_mgr` (`dnf` / `yum` /
  `apt`). Adding a new family means adding all three entries.
- **System-default wiring.** CPython is symlinked as `/usr/local/bin/python3`
  and `/usr/local/bin/python` (PATH already prefers `/usr/local/bin`). GCC is
  symlinked into `/usr/bin/{gcc,g++,cpp,gcov,c++}` to override the base
  image's toolchain. If a base image puts its system compiler somewhere other
  than `/usr/bin`, the override step needs revisiting.
- **GCC prerequisites** are fetched via `./contrib/download_prerequisites`.
  Distro `-devel` packages for gmp/mpfr/mpc/isl are installed as a fallback
  and to satisfy other build deps.
- **Filename derivation is mechanical** — `os_slug`, `py_slug`, `gcc_slug` come
  straight from config; arch comes from the host. Don't let the output name
  drift from the actual installed versions.

## Operational gotchas

- Building GCC from source is slow and memory-hungry. `--disable-bootstrap` in
  the example config is intentional; removing it triples build time.
- `docker export` does not preserve image metadata (CMD, ENV, etc.). Anyone
  who `docker import`s the tarball needs to re-specify `CMD`/`ENTRYPOINT` if
  they want one. This is by design — the artifact is meant to be a filesystem,
  not a runnable image.
- The script does not run docker as root automatically. If the user's docker
  needs sudo, that's on the caller's environment.
