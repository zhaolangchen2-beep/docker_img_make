"""Multi-CPython + dual-GCC renderer.

Builds multiple CPython versions side-by-side (each in its own prefix),
and keeps both the yum-installed GCC and source-compiled GCC switchable
via update-alternatives.
"""

from ._common import (DEFAULT_DEPS, LCOV_PERL_DEPS, PKG_CACHE_CLEAN,
                       PKG_INSTALL, insecure_install_cmd, job_expr,
                       py_major_minor, render_coverage_stage, wget_cmd)


def render_multi(cfg, base_ref=None) -> str:
    """Render Dockerfile with multiple CPython versions and dual GCC."""

    deps = DEFAULT_DEPS[cfg.pkg_mgr] + cfg.extra_packages
    if cfg.coverage:
        deps += LCOV_PERL_DEPS[cfg.pkg_mgr]
    deps_str = " ".join(deps)
    install = (insecure_install_cmd(cfg.pkg_mgr)
               if cfg.insecure_ssl else PKG_INSTALL[cfg.pkg_mgr])
    pkg_clean = PKG_CACHE_CLEAN[cfg.pkg_mgr]
    wget = wget_cmd(cfg.insecure_ssl)
    j_expr = job_expr(cfg.jobs)
    py_cfg = " ".join(cfg.py_configure)
    gcc_cfg = " ".join(cfg.gcc_configure)

    coverage_stage = render_coverage_stage(cfg.insecure_ssl) if cfg.coverage else ""

    from_ref = base_ref or cfg.base_image

    # ---- CPython stages ----
    python_stages: list[str] = []
    first_mm: str | None = None
    for ver in cfg.py_versions:
        mm = py_major_minor(ver)
        if first_mm is None:
            first_mm = mm
        prefix = f"/usr/local/cpython-{mm}"
        url = cfg.py_source_url_template.format(version=ver)
        python_stages.append(f"""\
# ---- Build & install CPython {ver} → {prefix} ----
RUN set -eux; \\
    mkdir -p /usr/local/src && cd /usr/local/src; \\
    {wget} -O python.tar.gz "{url}"; \\
    tar -xf python.tar.gz; \\
    rm -f python.tar.gz; \\
    cd Python-{ver}; \\
    ./configure --prefix={prefix} {py_cfg}; \\
    make -j{j_expr}; \\
    make install; \\
    ldconfig || true; \\
    cd / && rm -rf /usr/local/src/Python-{ver}; \\
    {prefix}/bin/python3 --version""")

    # ---- GCC stage ----
    gcc_ver = cfg.gcc_version
    gcc_stage = f"""\
# ---- Save yum GCC, build source GCC {gcc_ver}, set up alternatives ----
RUN set -eux; \\
    YUM_GCC_VER=$(gcc -dumpversion | cut -d. -f1); \\
    echo "detected yum GCC major version: $YUM_GCC_VER"; \\
    for tool in gcc g++ cpp gcov c++; do \\
        if [ -f /usr/bin/$tool ] && [ ! -L /usr/bin/$tool ]; then \\
            cp /usr/bin/$tool /usr/bin/$tool-$YUM_GCC_VER; \\
        fi; \\
    done; \\
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
        yum_path="/usr/bin/$tool-$YUM_GCC_VER"; \\
        src_path="/usr/local/bin/$tool"; \\
        if [ -f "$yum_path" ]; then \\
            update-alternatives --install /usr/bin/$tool $tool "$yum_path" 10; \\
        fi; \\
        if [ -f "$src_path" ]; then \\
            update-alternatives --install /usr/bin/$tool $tool "$src_path" 20; \\
        fi; \\
    done; \\
    update-alternatives --auto gcc; \\
    echo "=== source GCC ==="; /usr/local/bin/gcc --version; \\
    echo "=== yum GCC ==="; $(readlink -f /usr/bin/gcc-$YUM_GCC_VER) --version || true; \\
    echo "=== active default ==="; gcc --version"""

    # ---- Default python3 symlinks ----
    default_py_stage = f"""\
# ---- Set default python3 → CPython {cfg.py_versions[0]} ----
RUN set -eux; \\
    ln -sf /usr/local/cpython-{first_mm}/bin/python3 /usr/local/bin/python3; \\
    ln -sf /usr/local/cpython-{first_mm}/bin/python3 /usr/local/bin/python; \\
    ln -sf /usr/local/cpython-{first_mm}/bin/pip3 /usr/local/bin/pip3 2>/dev/null || true; \\
    ln -sf /usr/local/cpython-{first_mm}/bin/pip3 /usr/local/bin/pip 2>/dev/null || true; \\
    python3 --version"""

    py_blocks = "\n".join(python_stages)

    return f"""FROM {from_ref}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# ---- 1. Build deps ----
RUN {install} {deps_str} && {pkg_clean}
{coverage_stage}
{py_blocks}

{default_py_stage}

{gcc_stage}

# ---- Slim ----
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
