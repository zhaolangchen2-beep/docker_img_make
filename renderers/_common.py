"""Shared helpers for Dockerfile rendering."""

import platform

# uname -m → LLVM release asset arch suffix
LLVM_ASSET_ARCH = {
    "x86_64": "X64",
    "aarch64": "ARM64",
}

# ---- Package-manager dispatch tables ----

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


# ---- Architecture & version helpers ----

def host_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def py_major_minor(version: str) -> str:
    parts = version.split(".")
    return f"{parts[0]}.{parts[1]}"


def job_expr(jobs: int) -> str:
    """Return shell expression for parallel build jobs."""
    return "$(nproc)" if jobs == 0 else str(jobs)


def wget_cmd(insecure: bool) -> str:
    return "wget --no-check-certificate" if insecure else "wget"


# ---- lcov Perl deps (per package manager) ----

LCOV_PERL_DEPS = {
    "dnf": [
        "perl", "perl-Capture-Tiny", "perl-DateTime",
        "perl-JSON-XS", "perl-Regexp-Common",
    ],
    "yum": [
        "perl", "perl-Capture-Tiny", "perl-DateTime",
        "perl-JSON-XS", "perl-Regexp-Common",
    ],
    "apt": [
        "perl", "libcapture-tiny-perl", "libdatetime-perl",
        "libjson-xs-perl", "libregexp-common-perl",
    ],
}


# ---- Coverage stage (lcov 2.1, shared by both renderers) ----

def render_coverage_stage(insecure_ssl: bool) -> str:
    """Render the lcov 2.1 build-and-install stage."""
    wget = wget_cmd(insecure_ssl)
    curl_insecure = " -k" if insecure_ssl else ""
    return f"""\
# ---- Install lcov 2.1 → /usr/local/bin ----
RUN set -eux; \\
    cd /tmp; \\
    curl{curl_insecure} -L -o lcov-2.1.tar.gz "https://github.com/linux-test-project/lcov/releases/download/v2.1/lcov-2.1.tar.gz"; \\
    tar -xf lcov-2.1.tar.gz; \\
    cd lcov-2.1; \\
    make install PREFIX=/usr/local; \\
    cd / && rm -rf /tmp/lcov-2.1 /tmp/lcov-2.1.tar.gz; \\
    /usr/local/bin/lcov --version; \\
    /usr/local/bin/genhtml --version"""


# ---- LLVM stage (used by single renderer only) ----

def render_llvm_stage(py_jit: bool, py_llvm_version: str,
                      py_llvm_url: str, insecure_ssl: bool) -> tuple[str, str]:
    """Return (llvm_stage_str, path_prefix_str) for JIT-enabled single builds."""
    if not py_jit:
        return "", ""
    arch = host_arch()
    if py_llvm_url:
        llvm_url = py_llvm_url
    else:
        llvm_arch = LLVM_ASSET_ARCH.get(arch)
        if llvm_arch is None:
            raise SystemExit(
                f"unsupported arch {arch!r} for default LLVM URL; "
                f"set cpython.llvm_url or cpython.jit=false"
            )
        llvm_url = (
            f"https://github.com/llvm/llvm-project/releases/download/"
            f"llvmorg-{py_llvm_version}/LLVM-{py_llvm_version}-Linux-{llvm_arch}.tar.xz"
        )
    wget = wget_cmd(insecure_ssl)
    stage = f"""
# ---- 1b. LLVM {py_llvm_version} (build-time only, for CPython JIT; removed during slim) ----
RUN set -eux; \\
    mkdir -p /opt; \\
    cd /tmp; \\
    {wget} -O llvm.tar.xz "{llvm_url}"; \\
    mkdir -p /opt/llvm-{py_llvm_version}; \\
    tar -xf llvm.tar.xz -C /opt/llvm-{py_llvm_version} --strip-components=1; \\
    rm -f llvm.tar.xz; \\
    ln -sfn /opt/llvm-{py_llvm_version} /opt/llvm; \\
    /opt/llvm/bin/clang --version
ENV PATH=/opt/llvm/bin:$PATH
"""
    return stage, "PATH=/opt/llvm/bin:$PATH "
