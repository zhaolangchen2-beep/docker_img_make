#!/usr/bin/env python3
"""Build a slim OS image bundling CPython and GCC, both built from source.

Usage:
    ./build.py --config config.toml [--output-dir ./out] [--keep-image]

Requires: docker on the host, Python 3.11+ (for tomllib).
The image is built for the host's architecture (no cross-build).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# CPython minor → matching LLVM version for the JIT (PEP 744).
DEFAULT_LLVM_FOR_CPYTHON = {
    "3.13": "18.1.8",
    "3.14": "19.1.7",
}


@dataclass
class Config:
    base_source: str          # "registry" | "tarball"
    base_image: str           # registry ref (when source=registry)
    base_image_url: str       # tarball URL (when source=tarball)
    os_slug: str
    pkg_mgr: str
    renderer_name: str        # "single" (default) | "multi"
    # single-mode CPython
    py_version: str
    py_slug: str
    py_url: str
    py_jit: bool
    py_llvm_version: str
    py_llvm_url: str
    # multi-mode CPython
    py_versions: list[str]
    py_source_url_template: str
    # common CPython
    py_configure: list[str]
    # GCC
    gcc_version: str
    gcc_slug: str
    gcc_url: str
    gcc_configure: list[str]
    # build
    jobs: int
    extra_packages: list[str]
    insecure_ssl: bool
    coverage: bool
    # proxy
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
            renderer_name = build.get("renderer", "single")

            if renderer_name == "single":
                cfg = cls(
                    base_source=source,
                    base_image=base.get("image", ""),
                    base_image_url=base.get("image_url", ""),
                    os_slug=base["os_slug"],
                    pkg_mgr=base.get("pkg_mgr", "dnf"),
                    renderer_name="single",
                    py_version=cpy["version"],
                    py_slug=cpy["slug"],
                    py_url=cpy["source_url"],
                    py_configure=list(cpy.get("configure_args", [])),
                    py_jit=bool(cpy.get("jit", False)),
                    py_llvm_version=str(cpy.get("llvm_version", "")),
                    py_llvm_url=str(cpy.get("llvm_url", "")).strip(),
                    py_versions=[],
                    py_source_url_template="",
                    gcc_version=gcc["version"],
                    gcc_slug=gcc["slug"],
                    gcc_url=gcc["source_url"],
                    gcc_configure=list(gcc.get("configure_args", [])),
                    jobs=int(build.get("jobs", 0)),
                    extra_packages=list(build.get("extra_packages", [])),
                    insecure_ssl=bool(build.get("insecure_ssl", False)),
                    coverage=bool(build.get("coverage", False)),
                    proxy_http=str(proxy.get("http", "")).strip(),
                    proxy_https=str(proxy.get("https", "")).strip(),
                    proxy_no=str(proxy.get("no", "")).strip(),
                )
                if not cfg.py_slug:
                    sys.exit("config error: cpython.slug is required when renderer='single'")
                if not cfg.py_url:
                    sys.exit("config error: cpython.source_url is required when renderer='single'")
                if not cfg.py_version:
                    sys.exit("config error: cpython.version is required when renderer='single'")
            elif renderer_name == "multi":
                py_versions_raw = cpy.get("versions", [])
                py_slugs_raw = cpy.get("slugs", [])
                if not py_versions_raw:
                    sys.exit("config error: cpython.versions is required when renderer='multi'")
                if not py_slugs_raw:
                    sys.exit("config error: cpython.slugs is required when renderer='multi'")
                if len(py_versions_raw) != len(py_slugs_raw):
                    sys.exit("config error: cpython.versions and cpython.slugs must have the same length")
                py_versions = [str(v) for v in py_versions_raw]
                py_slugs = [str(s) for s in py_slugs_raw]
                source_url_template = cpy.get("source_url_template", "")
                if not source_url_template:
                    sys.exit("config error: cpython.source_url_template is required when renderer='multi'")
                # Validate template can render with first version
                try:
                    source_url_template.format(version=py_versions[0])
                except KeyError as e:
                    sys.exit(f"config error: cpython.source_url_template uses unsupported placeholder {e}")
                cfg = cls(
                    base_source=source,
                    base_image=base.get("image", ""),
                    base_image_url=base.get("image_url", ""),
                    os_slug=base["os_slug"],
                    pkg_mgr=base.get("pkg_mgr", "dnf"),
                    renderer_name="multi",
                    py_version="",  # unused in multi
                    py_slug="_".join(py_slugs),
                    py_url="",  # unused in multi
                    py_configure=list(cpy.get("configure_args", [])),
                    py_jit=False,  # multi mode: no JIT
                    py_llvm_version="",
                    py_llvm_url="",
                    py_versions=py_versions,
                    py_source_url_template=source_url_template,
                    gcc_version=gcc["version"],
                    gcc_slug=gcc["slug"],
                    gcc_url=gcc["source_url"],
                    gcc_configure=list(gcc.get("configure_args", [])),
                    jobs=int(build.get("jobs", 0)),
                    extra_packages=list(build.get("extra_packages", [])),
                    insecure_ssl=bool(build.get("insecure_ssl", False)),
                    coverage=bool(build.get("coverage", False)),
                    proxy_http=str(proxy.get("http", "")).strip(),
                    proxy_https=str(proxy.get("https", "")).strip(),
                    proxy_no=str(proxy.get("no", "")).strip(),
                )
            else:
                sys.exit(f"config error: unknown renderer {renderer_name!r}")
        except KeyError as e:
            sys.exit(f"config error: missing key {e}")

        from renderers._common import DEFAULT_DEPS
        if cfg.pkg_mgr not in DEFAULT_DEPS:
            sys.exit(f"config error: unsupported pkg_mgr {cfg.pkg_mgr!r}")
        if cfg.base_source not in ("registry", "tarball"):
            sys.exit(f"config error: base.source must be 'registry' or 'tarball', got {cfg.base_source!r}")
        if cfg.base_source == "registry" and not cfg.base_image:
            sys.exit("config error: base.image is required when source='registry'")
        if cfg.base_source == "tarball" and not cfg.base_image_url:
            sys.exit("config error: base.image_url is required when source='tarball'")

        # JIT LLVM version auto-detection (single mode only)
        if cfg.renderer_name == "single" and cfg.py_jit and not cfg.py_llvm_version:
            from renderers._common import py_major_minor
            mm = py_major_minor(cfg.py_version)
            llvm = DEFAULT_LLVM_FOR_CPYTHON.get(mm)
            if not llvm:
                sys.exit(
                    f"config error: cpython.jit=true but no default LLVM mapping for "
                    f"CPython {mm}; set cpython.llvm_version explicitly"
                )
            cfg.py_llvm_version = llvm
        return cfg


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


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def build_and_export(cfg: Config, output_dir: Path, keep_image: bool) -> Path:
    if not shutil.which("docker"):
        sys.exit("error: docker not found on PATH")

    from renderers._common import host_arch
    from renderers import get_renderer

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

    renderer = get_renderer(cfg.renderer_name)
    dockerfile_str = renderer(cfg, base_ref=base_ref)

    dockerfile = workdir / "Dockerfile"
    dockerfile.write_text(dockerfile_str)
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
        from renderers import get_renderer
        renderer = get_renderer(cfg.renderer_name)
        print(renderer(cfg))
        return
    build_and_export(cfg, args.output_dir, args.keep_image)


if __name__ == "__main__":
    main()
