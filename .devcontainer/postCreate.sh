#!/bin/bash

cd /workspaces/pyisyox

# Setup the example folder as copy of the example.
mkdir -p example
cp -r pyisyox/__main__.py example/example_connection.py

# Install the editable local package
pip3 install -e .
pip3 install -r requirements-dev.txt

# Install pre-commit requirements
pre-commit install --install-hooks
