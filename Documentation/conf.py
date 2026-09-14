# Sphinx configuration for the DeMoD Communication Framework docs.
import pathlib
import shutil

project = "DeMoD Communication Framework"
copyright = "2026, DeMoD LLC"
author = "DeMoD LLC"
release = "5.0.0"

extensions = [
    "myst_parser",
    "sphinx_design",
    "sphinx_copybutton",
    "sphinx.ext.githubpages",   # emits .nojekyll so _static/ is served by Pages
]

myst_enable_extensions = [
    "colon_fence", "deflist", "attrs_inline", "substitution", "tasklist",
    "linkify", "fieldlist",
]
myst_heading_anchors = 3

templates_path = ["_templates"]
html_static_path = ["_static"]
html_css_files = ["custom.css"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
suppress_warnings = ["myst.header", "myst.xref_missing", "toc.not_readable"]

# ── Stage external markdown (repo root + matrix-bridge) into _include/ so the
#    site can feature docs that live outside Documentation/. Runs on every build,
#    local or CI; _include/ is generated (gitignored).
_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent
_INC = _HERE / "_include"
_INC.mkdir(exist_ok=True)
for _rel, _dest in [
    ("README.md", "readme.md"),
    ("ARCHITECTURE.md", "architecture.md"),
    ("matrix-bridge/AGENT_TO_AGENT.md", "agent-to-agent.md"),
    ("CONTRIBUTING.md", "contributing.md"),
]:
    _src = _ROOT / _rel
    if _src.exists():
        shutil.copy(_src, _INC / _dest)

# ── HTML / theme ──────────────────────────────────────────────────────────────
html_theme = "furo"
html_title = "DeMoD Communication Framework"
html_baseurl = "https://alh477.github.io/Punctim/"
html_show_sphinx = False
pygments_style = "friendly"
pygments_dark_style = "dracula"

html_theme_options = {
    "sidebar_hide_name": False,
    "navigation_with_keys": True,
    "source_repository": "https://github.com/ALH477/Punctim",
    "source_branch": "main",
    "source_directory": "Documentation/",
    # Signal-cyan brand over deep-indigo ink — see _static/custom.css for the
    # full palette, fonts, and hero styling.
    "light_css_variables": {
        "color-brand-primary": "#0c8aa6",
        "color-brand-content": "#0c8aa6",
    },
    "dark_css_variables": {
        "color-brand-primary": "#34d6e8",
        "color-brand-content": "#34d6e8",
    },
}
