"""Utilities for outputting results to file."""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def write_to_file(xml_dict: dict, path: str) -> None:
    """Write the parse results to file for debugging."""
    file_path = Path(path)
    if parent := file_path.parent:
        parent.mkdir(parents=True, exist_ok=True)

    # JSON dumps used to serialize everything as as string since these
    # YAML files will not be parsed again
    json_object = json.dumps(xml_dict, indent=4, default=str)
    with open(
        path,
        "w",
        encoding="utf-8",
    ) as outfile:
        yaml.safe_dump(json.loads(json_object), outfile)
