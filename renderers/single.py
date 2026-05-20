"""Original single-CPython + single-source-GCC renderer."""

from ._common import (DEFAULT_DEPS, PKG_CACHE_CLEAN, PKG_INSTALL,
                       insecure_install_cmd, job_expr, py_major_minor,
                       render_llvm_stage, wget_cmd)


def render_single(cfg, base_ref=None) -> str:
    """Render the original Dockerfile (single CPython, single source GCC)."""

    deps = DEFAULT_DEPS[cfg.pkg_mgr] + cfg.extra_packages
    deps_str = " ".join(deps)
    install = (insecure_install_cmd(cfg.pkg_mgr)
               if cfg.insecure_ssl else PKG_INSTALL[cfg.pkg_mgr])
    pkg_clean = PKG_CACHE_CLEAN[cfg.pkg_mgr]
    wget = wget_cmd(cfg.insecure_ssl)
    j_expr = job_expr(cfg.jobs)

    py_mm = py_major_minor(cfg.py_version)

    # JIT: only inject the flag when enabled. Drop any user-supplied
    # --enable-experimental-jit so the `jit` toggle is the single source of truth.
    py_args = [a for a in cfg.py_configure
               if not a.startswith("--enable-experimental-jit")]
    if cfg.py_jit:
        py_args.append("--enable-experimental-jit=yes-off")
    py_cfg = " ".join(py_args)
    gcc_cfg = " ".join(cfg.gcc_configure)

    llvm_stage, py_path_prefix = render_llvm_stage(
        cfg.py_jit, cfg.py_llvm_version, cfg.py_llvm_url, cfg.insecure_ssl,
    )

    from_ref = base_ref or cfg.base_image
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
