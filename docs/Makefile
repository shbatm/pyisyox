# Minimal makefile for Sphinx documentation
SPHINXOPTS    ?= -n
SPHINXBUILD   ?= sphinx-build
SOURCEDIR     = .
BUILDDIR      = _build

help:
	@$(SPHINXBUILD) -M help "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)

# Regenerate docs/api/*.rst from the current package layout. Run this
# whenever new modules are added under pyisyox/ so the API reference
# stays in sync.
apidoc:
	rm -rf api
	sphinx-apidoc -f -o api -e -M -H "API Reference" --tocfile index \
	    ../pyisyox ../pyisyox/__main__.py

# Convenience: install all build deps, regenerate API, build HTML.
fresh: apidoc html

.PHONY: help apidoc fresh Makefile

%: Makefile
	@$(SPHINXBUILD) -M $@ "$(SOURCEDIR)" "$(BUILDDIR)" $(SPHINXOPTS) $(O)
