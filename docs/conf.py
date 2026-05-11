"""Configuration file for the Sphinx documentation builder."""
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
import os
import sys

import sphinx_rtd_theme

sys.path.insert(0, os.path.abspath(".."))

# Modules autodoc would import but that aren't always present in the
# docs build environment. aiohttp is the only hard dep beyond stdlib.
autodoc_mock_imports = ["aiohttp"]


# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "pyisyox"
copyright = "2026, shbatm"
author = "shbatm"
release = "6.0.0"

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "sphinx.ext.todo",
    "sphinx.ext.viewcode",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

# Google-style docstrings are used throughout the codebase.
napoleon_google_docstring = True
napoleon_numpy_docstring = False

# Reference Python stdlib and aiohttp.
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "aiohttp": ("https://docs.aiohttp.org/en/stable/", None),
}

# Render type hints as part of the description.
autodoc_typehints = "description"
autodoc_member_order = "bysource"

# The package re-exports symbols at the top level (e.g. ``pyisyox.Node``)
# alongside their submodule home (``pyisyox.runtime.node.Node``); both
# get autodoc'd, which trips the "more than one target" warning on
# cross-references. Suppress those cosmetic warnings; explicit refs in
# the prose docs use the ``pyisyox.X`` form and resolve unambiguously.
suppress_warnings = ["ref.python", "ref.exc", "autosectionlabel.*"]

# Napoleon expands "Attributes:" docstring sections into rST field lists,
# while autodoc also documents the dataclass attributes themselves —
# producing duplicate object descriptions. We document each attribute
# exactly once via napoleon (which keeps the prose alongside the type)
# and ignore the duplicate warnings autodoc emits as a side effect.
napoleon_use_ivar = True

# Don't repeat the full module path in front of every name in API pages
# — keeps the rendered class signatures readable.
add_module_names = False

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = "sphinx_rtd_theme"

# Add any paths that contain custom themes here, relative to this directory.
html_theme_path = [sphinx_rtd_theme.get_html_theme_path()]

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ["_static"]

# The short X.Y version.
# version = '1.0'
# The full version, including alpha/beta/rc tags.
# release = '1.0.5'

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = "sphinx"

# If true, `todo` and `todoList` produce output, else they produce nothing.
todo_include_todos = False
