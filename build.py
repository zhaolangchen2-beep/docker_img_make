#!/usr/bin/env python3
"""Build a slim OS image bundling a specific CPython and GCC, both built from source.

Usage:
    ./build.py --config config.toml [--output-dir ./out] [--keep-image]

Requires: docker on the host, Python 3.11+ (for tomllib).
The image is built for the host's architecture (no cross-build).
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEPS = {
    "dnf": [
        "gcc", "gcc-c++", "make", "wget", "tar", "xz", "bzip2", "patch",
        "diffutils", "findutils", "which", "file", "ca-certificates",
        "zlib-devel", "bzip2-devel", "xz-devel", "openssl-devel",
        "libffi-devel", "readline-devel", "sqlite-devel", "ncurses-devel",
        "tk-devel", "gdbm-devel", "libuuid-devel",
        "gmp-devel", "mpfr-devel", "libmpc-devel", "isl-devel",
        "flex", "bison", "texinfo",
    ],
    "yum": [
        "gcc", "gcc-c++", "make", "wget", "tar", "xz", "bzip2", "patch",
        "diffutils", "findutils", "which", "file", "ca-certificates",
        "zlib-devel", "bzip2-devel", "xz-devel", "openssl-devel",
        "libffi-devel", "readline-devel", "sqlite-devel", "ncurses-devel",
        "gmp-devel", "mpfr-devel", "libmpc-devel",
        "flex", "bison",
    ],
    "apt": [
        "build-essential", "wget", "ca-certificates", "xz-utils", "bzip2",
        "patch", "file",
        "zlib1g-dev", "libbz2-dev", "liblzma-dev", "libssl-dev", "libffi-dev",
        "libreadline-dev", "libsqlite3-dev", "libncurses-dev", "tk-dev",
        "libgdbm-dev", "uuid-dev",
        "libgmp-dev", "libmpfr-dev", "libmpc-dev", "libisl-dev",
        "flex", "bison", "texinfo",
    ],
}

PKG_INSTALL = {
    "dnf": "dnf install -y --setopt=install_weak_deps=False",
    "yum": "yum install -y",
    "apt": "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends",
}

PKG_CACHE_CLEAN = {
    "dnf": "dnf clean all && rm -rf /var/cache/dnf /var/cache/yum",
    "yum": "yum clean all && rm -rf /var/cache/yum",
    "apt": "apt-get clean && rm -rf /var/lib/apt/lists/*",
}


@dataclass
class Config:
    base_source: str          # "registry" | "tarball"
    base_image: str           # registry ref (when source=registry)
    base_image_url: str       # tarball URL (when source=tarball)
    os_slug: str
    pkg_mgr: str
    py_version: str
    py_slug: str
    py_url: str
    py_configure: list[str]
    gcc_version: str
    gcc_slug: str
    gcc_url: str
    gcc_configure: list[str]
    jobs: int
    extra_packages: list[str]

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open("rb") as f:
            data = tomllib.load(f)
        try:
            base = data["base"]
            cpy = data["cpython"]
            gcc = data["gcc"]
            build = data.get("build", {})
            source = base.get("source", "registry")
            cfg = cls(
                base_source=source,
                base_image=base.get("image", ""),
                base_image_url=base.get("image_url", ""),
                os_slug=base["os_slug"],
                pkg_mgr=base.get("pkg_mgr", "dnf"),
                py_version=cpy["version"],
                py_slug=cpy["slug"],
                py_url=cpy["source_url"],
                py_configure=list(cpy.get("configure_args", [])),
                gcc_version=gcc["version"],
                gcc_slug=gcc["slug"],
                gcc_url=gcc["source_url"],
                gcc_configure=list(gcc.get("configure_args", [])),
                jobs=int(build.get("jobs", 0)),
                extra_packages=list(build.get("extra_packages", [])),
            )
        except KeyError as e:
            sys.exit(f"config error: missing key {e}")
        if cfg.pkg_mgr not in DEFAULT_DEPS:
            sys.exit(f"config error: unsupported pkg_mgr {cfg.pkg_mgr!r}")
        if cfg.base_source not in ("registry", "tarball"):
            sys.exit(f"config error: base.source must be 'registry' or 'tarball', got {cfg.base_source!r}")
        if cfg.base_source == "registry" and not cfg.base_image:
            sys.exit("config error: base.image is required when source='registry'")
        if cfg.base_source == "tarball" and not cfg.base_image_url:
            sys.exit("config error: base.image_url is required when source='tarball'")
        return cfg


def host_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def py_major_minor(version: str) -> str:
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}"


def render_dockerfile(cfg: Config, base_ref: str | None = None) -> str:
    """`base_ref` overrides the FROM line — used for tarball-imported images."""
    deps = DEFAULT_DEPS[cfg.pkg_mgr] + cfg.extra_packages
    deps_str = " ".join(deps)
    install = PKG_INSTALL[cfg.pkg_mgr]
    pkg_clean = PKG_CACHE_CLEAN[cfg.pkg_mgr]
    jobs = cfg.jobs if cfg.jobs > 0 else 0  # 0 → use $(nproc) at runtime
    j_expr = "$(nproc)" if jobs == 0 else str(jobs)

    py_mm = py_major_minor(cfg.py_version)
    py_cfg = " ".join(cfg.py_configure)
    gcc_cfg = " ".join(cfg.gcc_configure)

    from_ref = base_ref or cfg.base_image
    # Single-RUN strategy per stage so removals actually shrink the export.
    return f"""# syntax=docker/dockerfile:1
FROM {from_ref}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# ---- 1. Build deps ----
RUN {install} {deps_str} && {pkg_clean}

# ---- 2. Build & install CPython {cfg.py_version}, set as default ----
RUN set -eux; \\
    mkdir -p /usr/local/src && cd /usr/local/src; \\
    wget -O python.tar.gz "{cfg.py_url}"; \\
    tar -xf python.tar.gz; \\
    rm -f python.tar.gz; \\
    cd Python-{cfg.py_version}; \\
    ./configure {py_cfg}; \\
    make -j{j_expr}; \\
    make install; \\
    ldconfig || true; \\
    ln -sf /usr/local/bin/python{py_mm} /usr/local/bin/python3; \\
    ln -sf /usr/local/bin/python3 /usr/local/bin/python; \\
    ln -sf /usr/local/bin/pip{py_mm} /usr/local/bin/pip3 2>/dev/null || true; \\
    ln -sf /usr/local/bin/pip3 /usr/local/bin/pip 2>/dev/null || true; \\
    cd / && rm -rf /usr/local/src/Python-{cfg.py_version}; \\
    python3 --version

# ---- 3. Build & install GCC {cfg.gcc_version}, set as default ----
RUN set -eux; \\
    mkdir -p /usr/local/src && cd /usr/local/src; \\
    wget -O gcc-src.tar.xz "{cfg.gcc_url}"; \\
    mkdir gcc-src && tar -xf gcc-src.tar.xz -C gcc-src --strip-components=1; \\
    rm -f gcc-src.tar.xz; \\
    cd gcc-src; \\
    ./contrib/download_prerequisites || true; \\
    mkdir ../gcc-build && cd ../gcc-build; \\
    ../gcc-src/configure {gcc_cfg}; \\
    make -j{j_expr}; \\
    make install-strip; \\
    cd / && rm -rf /usr/local/src/gcc-src /usr/local/src/gcc-build; \\
    echo "/usr/local/lib64" > /etc/ld.so.conf.d/local-gcc.conf; \\
    echo "/usr/local/lib"   >> /etc/ld.so.conf.d/local-gcc.conf; \\
    ldconfig; \\
    for tool in gcc g++ cpp gcov c++; do \\
        if [ -x /usr/local/bin/$tool ]; then ln -sf /usr/local/bin/$tool /usr/bin/$tool; fi; \\
    done; \\
    gcc --version; g++ --version

# ---- 4. Slim ----
RUN set -eux; \\
    rm -rf /usr/local/src /tmp/* /var/tmp/* /root/.cache /root/.wget-hsts; \\
    find /usr/local/lib /usr/local/lib64 -name '*.la' -delete 2>/dev/null || true; \\
    find /usr/local -depth -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null || true; \\
    find /usr/local/lib -depth -type d -name test -exec rm -rf {{}} + 2>/dev/null || true; \\
    find /usr/local/lib -depth -type d -name tests -exec rm -rf {{}} + 2>/dev/null || true; \\
    rm -rf /usr/local/share/doc /usr/local/share/man /usr/local/share/info; \\
    rm -rf /usr/share/doc /usr/share/man /usr/share/info /usr/share/locale/* 2>/dev/null || true; \\
    {pkg_clean}

CMD ["/bin/bash"]
"""


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def import_base_tarball(url: str, tag: str, workdir: Path) -> str:
    """Download a base OS rootfs tarball and import it as a docker image."""
    workdir.mkdir(parents=True, exist_ok=True)
    fname = url.rstrip("/").split("/")[-1] or "base.tar"
    local = workdir / fname
    if not local.exists():
        print(f"[+] downloading base tarball {url}")
        run(["wget", "-O", str(local), url])
    else:
        print(f"[+] reusing cached base tarball {local}")
    print(f"[+] docker import → {tag}")
    run(["docker", "import", str(local), tag])
    return tag


def build_and_export(cfg: Config, output_dir: Path, keep_image: bool) -> Path:
    if not shutil.which("docker"):
        sys.exit("error: docker not found on PATH")
    arch = host_arch()
    name = f"{cfg.os_slug}-{cfg.py_slug}-{cfg.gcc_slug}-{arch}"
    image_tag = f"{name}:latest"
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{name}.tar.gz"

    workdir = output_dir / f".build-{name}"
    workdir.mkdir(exist_ok=True)

    base_ref: str | None = None
    if cfg.base_source == "tarball":
        base_tag = f"{cfg.os_slug}-base:imported"
        base_ref = import_base_tarball(cfg.base_image_url, base_tag, workdir / "base")

    dockerfile = workdir / "Dockerfile"
    dockerfile.write_text(render_dockerfile(cfg, base_ref=base_ref))
    print(f"[+] Dockerfile written to {dockerfile}")

    run(["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(workdir)])

    container = f"export-{name}"
    subprocess.run(["docker", "rm", "-f", container],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run(["docker", "create", "--name", container, image_tag])
    try:
        print(f"[+] Exporting → {out_path}")
        with out_path.open("wb") as out_f:
            export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
            gzip_p = subprocess.Popen(["gzip", "-9"], stdin=export.stdout, stdout=out_f)
            assert export.stdout is not None
            export.stdout.close()
            if gzip_p.wait() != 0 or export.wait() != 0:
                sys.exit("error: export | gzip failed")
    finally:
        subprocess.run(["docker", "rm", "-f", container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not keep_image:
            subprocess.run(["docker", "rmi", image_tag],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"[✓] {out_path}  ({size_mb:.1f} MiB)")
    return out_path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", "-c", required=True, type=Path)
    p.add_argument("--output-dir", "-o", default=Path("./out"), type=Path)
    p.add_argument("--keep-image", action="store_true",
                   help="Keep the intermediate docker image after exporting.")
    p.add_argument("--print-dockerfile", action="store_true",
                   help="Print the rendered Dockerfile and exit (no build).")
    args = p.parse_args()

    cfg = Config.load(args.config)
    if args.print_dockerfile:
        print(render_dockerfile(cfg))
        return
    build_and_export(cfg, args.output_dir, args.keep_image)


if __name__ == "__main__":
    main()
