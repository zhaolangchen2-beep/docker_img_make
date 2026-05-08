# docker_img_make

Build a slim OS image bundling a specific CPython and GCC, both compiled from
source, for the host's CPU architecture.

Output: `<os_slug>-<py_slug>-<gcc_slug>-<arch>.tar.gz`
Example: `oe2403sp1-py3143-gcc14-aarch64.tar.gz`

The tarball is a flattened container filesystem (`docker export`) and can be
loaded back with `docker import`.

## Requirements

- Linux host with `docker` on PATH
- Python 3.11+ (uses stdlib `tomllib`)
- Plenty of CPU and RAM — building GCC from source is slow

## Quick start

```bash
cp config.example.toml config.toml
# edit base image / versions / URLs / configure args as needed

./build.py --config config.toml
# → ./out/<os>-<py>-<gcc>-<arch>.tar.gz
```

Inspect the generated Dockerfile without building:

```bash
./build.py --config config.toml --print-dockerfile
```

Load the result on another host:

```bash
docker import out/oe2403sp1-py3143-gcc14-aarch64.tar.gz my/oe-py-gcc:latest
docker run --rm -it my/oe-py-gcc:latest python3 --version
docker run --rm -it my/oe-py-gcc:latest gcc --version
```

## Config

See `config.example.toml`. Key fields:

- `base.image` — base OS image to pull
- `base.os_slug` — short name used in the output filename
- `base.pkg_mgr` — `dnf`, `yum`, or `apt`
- `cpython.{version,slug,source_url,configure_args}`
- `gcc.{version,slug,source_url,configure_args}`
- `build.jobs` — `0` for `nproc`
- `build.extra_packages` — extra build deps on top of the per-pkgmgr defaults

## How it works

1. Render a Dockerfile from the config.
2. `docker build` it. Each phase (deps install / CPython / GCC / slim) is one
   `RUN` so source trees and caches are removed in the same layer.
3. `docker create` a container from the built image, then
   `docker export | gzip -9` it into the final tarball.
4. Remove the temp container and (unless `--keep-image`) the image.

CPython is symlinked as system `python3` / `python`; GCC is symlinked into
`/usr/bin` so the new toolchain is the default for child processes.
