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
import re
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

def insecure_install_cmd(pkg_mgr: str) -> str:
    """Variant of PKG_INSTALL[pkg_mgr] that disables TLS verification."""
    base = PKG_INSTALL[pkg_mgr]
    if pkg_mgr in ("dnf", "yum"):
        return base + " --setopt=sslverify=False"
    if pkg_mgr == "apt":
        opts = " -o Acquire::https::Verify-Peer=false -o Acquire::https::Verify-Host=false"
        return (base
                .replace("apt-get update", "apt-get update" + opts)
                .replace("apt-get install", "apt-get install" + opts))
    return base

PKG_CACHE_CLEAN = {
    "dnf": "dnf clean all && rm -rf /var/cache/dnf /var/cache/yum",
    "yum": "yum clean all && rm -rf /var/cache/yum",
    "apt": "apt-get clean && rm -rf /var/lib/apt/lists/*",
}

# CPython minor → matching LLVM version for the JIT (PEP 744).
DEFAULT_LLVM_FOR_CPYTHON = {
    "3.13": "18.1.8",
    "3.14": "19.1.7",
}

# uname -m → LLVM release asset arch suffix
LLVM_ASSET_ARCH = {
    "x86_64": "X64",
    "aarch64": "ARM64",
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
    py_jit: bool
    py_llvm_version: str
    py_llvm_url: str   # optional override; empty → use built-in template
    gcc_version: str
    gcc_slug: str
    gcc_url: str
    gcc_configure: list[str]
    jobs: int
    extra_packages: list[str]
    insecure_ssl: bool
    proxy_http: str
    proxy_https: str
    proxy_no: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open("rb") as f:
            data = tomllib.load(f)
        try:
            base = data["base"]
            cpy = data["cpython"]
            gcc = data["gcc"]
            build = data.get("build", {})
            proxy = data.get("proxy", {})
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
                py_jit=bool(cpy.get("jit", False)),
                py_llvm_version=str(cpy.get("llvm_version", "")),
                py_llvm_url=str(cpy.get("llvm_url", "")).strip(),
                gcc_version=gcc["version"],
                gcc_slug=gcc["slug"],
                gcc_url=gcc["source_url"],
                gcc_configure=list(gcc.get("configure_args", [])),
                jobs=int(build.get("jobs", 0)),
                extra_packages=list(build.get("extra_packages", [])),
                insecure_ssl=bool(build.get("insecure_ssl", False)),
                proxy_http=str(proxy.get("http", "")).strip(),
                proxy_https=str(proxy.get("https", "")).strip(),
                proxy_no=str(proxy.get("no", "")).strip(),
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
        if cfg.py_jit and not cfg.py_llvm_version:
            mm = py_major_minor(cfg.py_version)
            llvm = DEFAULT_LLVM_FOR_CPYTHON.get(mm)
            if not llvm:
                sys.exit(
                    f"config error: cpython.jit=true but no default LLVM mapping for "
                    f"CPython {mm}; set cpython.llvm_version explicitly"
                )
            cfg.py_llvm_version = llvm
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
    install = (insecure_install_cmd(cfg.pkg_mgr)
               if cfg.insecure_ssl else PKG_INSTALL[cfg.pkg_mgr])
    pkg_clean = PKG_CACHE_CLEAN[cfg.pkg_mgr]
    wget = "wget --no-check-certificate" if cfg.insecure_ssl else "wget"
    jobs = cfg.jobs if cfg.jobs > 0 else 0  # 0 → use $(nproc) at runtime
    j_expr = "$(nproc)" if jobs == 0 else str(jobs)

    py_mm = py_major_minor(cfg.py_version)

    # JIT: only inject the flag when enabled. Drop any user-supplied
    # --enable-experimental-jit so the `jit` toggle is the single source of truth.
    py_args = [a for a in cfg.py_configure
               if not a.startswith("--enable-experimental-jit")]
    if cfg.py_jit:
        py_args.append("--enable-experimental-jit=yes-off")
    py_cfg = " ".join(py_args)
    gcc_cfg = " ".join(cfg.gcc_configure)

    # LLVM install stage (only when JIT is on)
    llvm_stage = ""
    py_path_prefix = ""
    if cfg.py_jit:
        arch = host_arch()
        llvm_ver = cfg.py_llvm_version
        if cfg.py_llvm_url:
            llvm_url = cfg.py_llvm_url
        else:
            llvm_arch = LLVM_ASSET_ARCH.get(arch)
            if llvm_arch is None:
                raise SystemExit(
                    f"unsupported arch {arch!r} for default LLVM URL; "
                    f"set cpython.llvm_url or cpython.jit=false"
                )
            llvm_url = (
                f"https://github.com/llvm/llvm-project/releases/download/"
                f"llvmorg-{llvm_ver}/LLVM-{llvm_ver}-Linux-{llvm_arch}.tar.xz"
            )
        llvm_stage = f"""
# ---- 1b. LLVM {llvm_ver} (build-time only, for CPython JIT; removed during slim) ----
RUN set -eux; \\
    mkdir -p /opt; \\
    cd /tmp; \\
    {wget} -O llvm.tar.xz "{llvm_url}"; \\
    mkdir -p /opt/llvm-{llvm_ver}; \\
    tar -xf llvm.tar.xz -C /opt/llvm-{llvm_ver} --strip-components=1; \\
    rm -f llvm.tar.xz; \\
    ln -sfn /opt/llvm-{llvm_ver} /opt/llvm; \\
    /opt/llvm/bin/clang --version
ENV PATH=/opt/llvm/bin:$PATH
"""
        py_path_prefix = "PATH=/opt/llvm/bin:$PATH "

    from_ref = base_ref or cfg.base_image
    # Single-RUN strategy per stage so removals actually shrink the export.
    return f"""FROM {from_ref}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# ---- 1. Build deps ----
RUN {install} {deps_str} && {pkg_clean}
{llvm_stage}
# ---- 2. Build & install CPython {cfg.py_version}, set as default ----
RUN set -eux; \\
    mkdir -p /usr/local/src && cd /usr/local/src; \\
    {wget} -O python.tar.gz "{cfg.py_url}"; \\
    tar -xf python.tar.gz; \\
    rm -f python.tar.gz; \\
    cd Python-{cfg.py_version}; \\
    {py_path_prefix}./configure {py_cfg}; \\
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
    {wget} -O gcc-src.tar.xz "{cfg.gcc_url}"; \\
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
    rm -rf /opt/llvm /opt/llvm-* /usr/local/src /tmp/* /var/tmp/* /root/.cache /root/.wget-hsts; \\
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


def proxy_build_args(cfg: Config) -> list[str]:
    """Docker predefined proxy build-args. They transparently flow into every
    RUN as env vars but are NOT written to the image config, so they vanish
    automatically when we `docker export` the container."""
    args: list[str] = []
    pairs = [
        ("http_proxy", cfg.proxy_http),  ("HTTP_PROXY", cfg.proxy_http),
        ("https_proxy", cfg.proxy_https), ("HTTPS_PROXY", cfg.proxy_https),
        ("no_proxy", cfg.proxy_no),       ("NO_PROXY", cfg.proxy_no),
    ]
    for name, val in pairs:
        if val:
            args += ["--build-arg", f"{name}={val}"]
    return args


def proxy_env(cfg: Config) -> dict[str, str]:
    """Env for host-side downloads (wget for tarball base image)."""
    env = os.environ.copy()
    if cfg.proxy_http:
        env["http_proxy"] = env["HTTP_PROXY"] = cfg.proxy_http
    if cfg.proxy_https:
        env["https_proxy"] = env["HTTPS_PROXY"] = cfg.proxy_https
    if cfg.proxy_no:
        env["no_proxy"] = env["NO_PROXY"] = cfg.proxy_no
    return env


def detect_tar_format(path: Path) -> str:
    """Return 'archive' for a `docker save` tarball (has manifest.json at the
    top level), 'rootfs' otherwise (treat as a flat filesystem tarball)."""
    res = subprocess.run(
        ["tar", "-tf", str(path)],
        capture_output=True, text=True, check=True,
    )
    for line in res.stdout.splitlines()[:1000]:
        s = line.strip().lstrip("./").rstrip("/")
        if s == "manifest.json":
            return "archive"
    return "rootfs"


def load_or_import_base(url: str, tag: str, workdir: Path, env: dict[str, str],
                        insecure: bool = False) -> str:
    """Fetch a base OS tarball and surface it as a docker image ref.

    Auto-detects:
      - `docker save` archive (has manifest.json) → `docker load`, return the
        image tag from the load output (or the image ID, if untagged).
      - rootfs tarball → `docker import` under the requested tag.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    fname = url.rstrip("/").split("/")[-1] or "base.tar"
    local = workdir / fname
    if not local.exists():
        print(f"[+] downloading base tarball {url}")
        wget_cmd = ["wget"]
        if insecure:
            wget_cmd.append("--no-check-certificate")
        wget_cmd += ["-O", str(local), url]
        print("+", " ".join(wget_cmd), flush=True)
        subprocess.run(wget_cmd, check=True, env=env)
    else:
        print(f"[+] reusing cached base tarball {local}")

    fmt = detect_tar_format(local)
    print(f"[+] detected tarball format: {fmt}")
    if fmt == "archive":
        print(f"[+] docker load -i {local}")
        out = subprocess.check_output(
            ["docker", "load", "-i", str(local)], text=True,
        )
        sys.stdout.write(out)
        m = re.search(r"Loaded image(?: ID)?:\s*(\S+)", out)
        if not m:
            sys.exit(f"error: could not parse `docker load` output:\n{out}")
        loaded = m.group(1)
        # Re-tag under our slug so the FROM line is stable across runs even
        # when the upstream archive ships an untagged ID.
        run(["docker", "tag", loaded, tag])
        return tag

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

    env = proxy_env(cfg)
    base_ref: str | None = None
    if cfg.base_source == "tarball":
        base_tag = f"{cfg.os_slug}-base:imported"
        base_ref = load_or_import_base(
            cfg.base_image_url, base_tag, workdir / "base", env,
            insecure=cfg.insecure_ssl,
        )

    dockerfile = workdir / "Dockerfile"
    dockerfile.write_text(render_dockerfile(cfg, base_ref=base_ref))
    print(f"[+] Dockerfile written to {dockerfile}")

    build_cmd = ["docker", "build", *proxy_build_args(cfg),
                 "-t", image_tag, "-f", str(dockerfile), str(workdir)]
    run(build_cmd)

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
