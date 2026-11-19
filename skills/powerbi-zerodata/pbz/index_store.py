"""Completed category-index generations. Unpublished/legacy artifacts are never trusted."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid

from .common import PbzError, read_checked, write_text

FILES = ("index.json", "categories.bin", "index-summary.json")


def load(private_dir, project=None):
    """Read one immutable, hash-checked generation; None for missing or incomplete stores."""
    try:
        manifest = json.loads(read_checked(os.path.join(private_dir, "index-current.json"), root=private_dir))
        if not isinstance(manifest, dict) or manifest.get("complete") is not True:
            return None
        generation = manifest.get("generation")
        if not isinstance(generation, str) or not re.fullmatch(r"index-[0-9a-f]{32}", generation):
            return None
        if project is not None and manifest.get("project") != project:
            return None
        hashes = manifest.get("sha256")
        if not isinstance(hashes, dict):
            return None
        blobs = {}
        for name in FILES:
            raw = read_checked(os.path.join(private_dir, generation, name), root=private_dir)
            if hashlib.sha256(raw).hexdigest() != hashes.get(name):
                return None
            blobs[name] = raw
        return blobs
    except PbzError as err:
        if err.code == "UNSAFE_PATH" and os.path.lexists(os.path.join(private_dir, "index-current.json")):
            raise
        return None
    except (OSError, ValueError, UnicodeError):
        return None


def publish(private_dir, project, summary):
    """The manifest is the sole commit point, written only after hardening all artifacts."""
    generation = "index-" + uuid.uuid4().hex
    directory = os.path.join(private_dir, generation)
    os.mkdir(directory)
    hashes = {}
    for name in FILES:
        raw = (json.dumps(summary).encode("utf-8") if name == "index-summary.json" else
               read_checked(os.path.join(private_dir, name), root=private_dir))
        path = os.path.join(directory, name)
        with open(path, "xb") as handle:
            handle.write(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    manifest = {"complete": True, "generation": generation, "project": project, "sha256": hashes}
    write_text(os.path.join(private_dir, "index-current.json"), json.dumps(manifest), root=private_dir)
