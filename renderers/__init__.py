"""Renderer registry. Select via build.renderer in config."""

from .single import render_single
from .multi import render_multi

RENDERERS = {
    "single": render_single,
    "multi": render_multi,
}


def get_renderer(name: str):
    if name not in RENDERERS:
        raise SystemExit(
            f"config error: unknown renderer {name!r}; "
            f"choose from {list(RENDERERS)}"
        )
    return RENDERERS[name]
