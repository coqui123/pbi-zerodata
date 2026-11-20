"""Completed category-index generations. Unpublished/legacy artifacts are never trusted."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid

from .common import PbzError, _reparse_link, read_checked, write_text

FILES = ("index.json", "categories.bin", "index-summary.json")


def load(private_dir, project=None):
    """Read one immutable, hash-checked generation; None for missing or incomplete stores."""
    manifest_read = False
    try:
        raw_manifest = read_checked(os.path.join(private_dir, "index-current.json"), root=private_dir)
        manifest_read = True
        manifest = json.loads(raw_manifest)
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
        for attempt in range(2):
            try:
                blobs = {}
                for name in FILES:
                    raw = read_checked(os.path.join(private_dir, generation, name), root=private_dir)
                    if hashlib.sha256(raw).hexdigest() != hashes.get(name):
                        break
                    blobs[name] = raw
                else:
                    return blobs
            except (OSError, ValueError, UnicodeError, PbzError):
                pass
            if attempt:
                return None
            fresh = json.loads(read_checked(os.path.join(private_dir, "index-current.json"), root=private_dir))
            if not isinstance(fresh, dict) or fresh.get("generation") == generation:
                return None
            generation = fresh.get("generation")
            hashes = fresh.get("sha256")
            if (fresh.get("complete") is not True or not isinstance(generation, str)
                    or not re.fullmatch(r"index-[0-9a-f]{32}", generation)
                    or (project is not None and fresh.get("project") != project)
                    or not isinstance(hashes, dict)):
                return None
    except PbzError as err:
        if not manifest_read and err.code == "UNSAFE_PATH" and os.path.lexists(os.path.join(private_dir, "index-current.json")):
            raise
        return None
    except (OSError, ValueError, UnicodeError):
        return None


def load_strict(private_dir, project):
    """Refuse an unusable generation belonging to this project."""
    if not os.path.lexists(os.path.join(private_dir, "index-current.json")):
        return None
    try:
        raw = read_checked(os.path.join(private_dir, "index-current.json"), root=private_dir)
    except FileNotFoundError:
        return None
    except (OSError, PbzError):
        raise PbzError("REFUSED", "private index could not be read") from None
    try:
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("project"), str):
            raise ValueError()
        if manifest["project"] != project:
            return None
        stored = load(private_dir, project)
        if stored is None:
            raise ValueError()
        from . import dpapi
        json.loads(stored["index.json"])
        json.loads(stored["index-summary.json"])
        json.loads(dpapi.unprotect(stored["categories.bin"]))
        return stored
    except (OSError, ValueError, UnicodeError, PbzError):
        raise PbzError("REFUSED", "private index could not be read") from None


def publish(private_dir, project, summary, expected=None, blobs=None):
    """The manifest commits a complete generation with pinned hardened bytes."""
    supplied = blobs
    blobs = {}
    for name in FILES:
        raw = (json.dumps(summary).encode("utf-8") if name == "index-summary.json" else
               supplied[name] if supplied is not None else
               read_checked(os.path.join(private_dir, name), root=private_dir))
        if expected is not None and name in expected and hashlib.sha256(raw).hexdigest() != expected[name]:
            raise PbzError("REFUSED", "private index changed before publication")
        blobs[name] = raw
    generation = "index-" + uuid.uuid4().hex
    directory = os.path.join(private_dir, generation)
    os.mkdir(directory)
    try:
        hashes = {}
        for name, raw in blobs.items():
            with open(os.path.join(directory, name), "xb") as handle:
                handle.write(raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        manifest = {"complete": True, "generation": generation, "project": project, "sha256": hashes}
        write_text(os.path.join(private_dir, "index-current.json"), json.dumps(manifest), root=private_dir)
    except Exception:
        shutil.rmtree(directory)
        raise
    for entry in os.scandir(private_dir):
        if entry.name == generation or not re.fullmatch(r"index-[0-9a-f]{32}", entry.name):
            continue
        try:
            if _reparse_link(entry.path):
                os.unlink(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
        except OSError:
            pass
