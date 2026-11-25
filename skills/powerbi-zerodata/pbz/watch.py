"""Re-run mirror validation and best-practice analysis when asked.

Tracks SHA-256 digest of input set (mirror directory, policy.json, rule files)
and re-runs only when inputs change or --rescan is passed. Keeps a ring buffer
of N recent passes (default 50).

Single-session developer model:
Watch runs in a single developer session against local mirrors. It detects local
modifications, additions, and deletions between polls. It provides safe traversal
within workspace bounds, rejecting symlinks and reparse points strictly below
the workspace root (consistent with guard._reparse_blocked and common.refuse_below,
where the root itself may be reached through a junction on Dev Drives or redirected
profiles), and performs detection of in-flight root, intermediate ancestor, and directory
replacement during walk via dev/inode re-verification, failing closed on unexpected
filesystem mutations or link introductions.
Concurrent adversarial directory-swapping attacks by hostile unprivileged processes
outside the user's workspace are out of scope.

`--once` runs a single pass so tests, CI, and MCP do not loop. A human CLI session
that omits it polls until interrupted.
"""
from __future__ import annotations

from collections import deque
import hashlib
import os
import stat
import time

from . import bpa, common, guard, validate
from .common import PbzError


def _is_any_link(p: str) -> bool:
    """Check whether path is a symbolic link or Windows reparse point."""
    return os.path.islink(p) or guard._is_link(p) or common._reparse_link(p)


def _encode_rel_path(rel_path: str) -> str:
    """Encode relative path components unambiguously with length prefixes."""
    parts = rel_path.split(os.sep)
    return "/".join(f"{len(p)}:{p}" for p in parts)


def _check_containment_and_ancestors(path: str, stop_dir: str | None = None,
                                     ancestor_identities: dict[str, tuple[int, int]] | None = None,
                                     optional: bool = False,
                                     absent_ancestors: set[str] | None = None) -> str:
    """Verify lexical containment within stop_dir and ensure ancestors within stop_dir are link-free.
    Pins intermediate ancestor identities on first observation and rejects replacements.
    """
    abs_path = os.path.abspath(path)

    # Reject '..' parent traversals before resolving
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        common._refuse_dotdot_through_link(path)

    if stop_dir is not None:
        abs_stop = os.path.abspath(stop_dir)
        norm_stop = os.path.normcase(abs_stop)
        norm_path = os.path.normcase(abs_path)
        if norm_path == norm_stop:
            return abs_path

        try:
            rel = os.path.relpath(abs_path, abs_stop)
            if rel == ".." or rel.startswith(".." + os.sep) or (os.sep != "/" and rel.startswith("../")):
                raise PbzError("UNSAFE_PATH", "path outside workspace")
        except ValueError:
            raise PbzError("UNSAFE_PATH", "path outside workspace") from None

        # Collect ancestor components strictly below stop_dir down to abs_path's parent
        ancestors: list[str] = []
        cur = os.path.dirname(abs_path)
        while cur and os.path.normcase(cur) != norm_stop and len(cur) >= len(abs_stop):
            ancestors.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

        # Check ancestors top-down (from closest to stop_dir down to abs_path's parent)
        # Any existing ancestor below stop_dir MUST be verified for links and pinned.
        for anc in reversed(ancestors):
            if not os.path.lexists(anc):
                if optional:
                    if absent_ancestors is not None:
                        absent_ancestors.add(anc)
                    continue
                raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

            if _is_any_link(anc):
                raise PbzError("UNSAFE_PATH", "refusing to scan link")

            try:
                st_cur = os.lstat(anc)
            except OSError:
                raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

            if not stat.S_ISDIR(st_cur.st_mode):
                raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")

            if ancestor_identities is not None:
                cur_id = (st_cur.st_dev, st_cur.st_ino)
                prev_id = ancestor_identities.setdefault(anc, cur_id)
                if prev_id != cur_id:
                    raise PbzError("UNSAFE_PATH", "intermediate ancestor replaced during scan")

    return abs_path


def _inspect_path(path: str, expect_dir: bool, optional: bool = False,
                  stop_dir: str | None = None,
                  ancestor_identities: dict[str, tuple[int, int]] | None = None,
                  absent_ancestors: set[str] | None = None) -> tuple[str, tuple[int, ...]] | None:
    """Inspect path safely using os.lstat and ancestor link verification without following symlinks.

    Returns:
        Tuple of (canonical absolute path, (st_dev, st_ino)) for dir if valid.
        Tuple of (canonical absolute path, (st_dev, st_ino, st_size, st_mtime_ns)) for file if valid.
        None if legitimately absent and optional=True.
    Raises:
        PbzError("UNSAFE_PATH"): if path or an ancestor is a link/reparse or unexpected object type.
        PbzError("SCAN_FAILED"): if stat fails due to permission or I/O error.
    """
    abs_path = _check_containment_and_ancestors(path, stop_dir=stop_dir, ancestor_identities=ancestor_identities,
                                                optional=optional, absent_ancestors=absent_ancestors)

    try:
        st = os.lstat(abs_path)
    except FileNotFoundError:
        if optional:
            return None
        raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
    except OSError:
        raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

    if _is_any_link(abs_path):
        raise PbzError("UNSAFE_PATH", "refusing to scan link")

    if expect_dir:
        if not stat.S_ISDIR(st.st_mode):
            raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")
        return abs_path, (st.st_dev, st.st_ino)
    else:
        if not stat.S_ISREG(st.st_mode):
            raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")
        return abs_path, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def tree_digest(ws, project) -> str:
    """Compute deterministic SHA-256 digest of input set: mirror files, policy.json, and rule files.

    Traverses strictly without following symlinks and enforces containment.
    Fails closed on any read or traversal error. Hashes explicit presence/absence
    records for roots and subdirectories.
    """
    ws_root = getattr(ws, "root", None)
    if not ws_root:
        raise PbzError("UNSAFE_PATH", "workspace root is required for watch")

    abs_ws_root = os.path.abspath(ws_root)
    try:
        st_ws = os.lstat(abs_ws_root)
    except OSError:
        raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
    if not stat.S_ISDIR(st_ws.st_mode):
        raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")
    ws_initial_id = (st_ws.st_dev, st_ws.st_ino)
    ws_initial_link = _is_any_link(abs_ws_root)

    ancestor_identities: dict[str, tuple[int, int]] = {}
    all_dir_identities: dict[str, tuple[int, int]] = {}
    all_dir_entries: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    all_file_identities: dict[str, tuple[int, int, int, int]] = {}
    all_absent_ancestors: set[str] = set()
    all_absent_paths: set[str] = set()
    h = hashlib.sha256()

    def _hash_file(prefix: str, rel_path: str, abs_path: str, root_dir: str,
                   expected_sig: tuple[int, int, int, int] | None = None) -> None:
        try:
            st_f = os.lstat(abs_path)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        if _is_any_link(abs_path):
            raise PbzError("UNSAFE_PATH", "refusing to scan link")
        if not stat.S_ISREG(st_f.st_mode):
            raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")

        file_sig = (st_f.st_dev, st_f.st_ino, st_f.st_size, st_f.st_mtime_ns)
        if expected_sig is not None and file_sig != expected_sig:
            raise PbzError("UNSAFE_PATH", "file modified or replaced during scan")

        prev_f_sig = all_file_identities.setdefault(abs_path, file_sig)
        if prev_f_sig != file_sig:
            raise PbzError("UNSAFE_PATH", "file modified or replaced during scan")

        norm_rel = _encode_rel_path(rel_path)
        h.update(f"{prefix}:file:{norm_rel}:".encode("utf-8", errors="surrogateescape"))
        try:
            data = common.read_checked(abs_path, root=root_dir)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        h.update(hashlib.sha256(data).digest())

    def _walk_dir(prefix: str, root_dir: str, root_stat: tuple[int, int]) -> None:
        try:
            st_before = os.lstat(root_dir)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

        if (st_before.st_dev, st_before.st_ino) != root_stat or _is_any_link(root_dir):
            raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")
        if not stat.S_ISDIR(st_before.st_mode):
            raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")

        dir_identities: dict[str, tuple[int, int]] = {root_dir: root_stat}
        visited_dirs: set[str] = set()

        def _on_error(err: OSError):
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

        for cur, dirs, files in os.walk(root_dir, followlinks=False, onerror=_on_error):
            visited_dirs.add(cur)
            entry = (tuple(sorted(dirs)), tuple(sorted(files)))
            if all_dir_entries.setdefault(cur, entry) != entry:
                raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")

            # Verify entered directory identity matches parent enumeration
            try:
                st_cur = os.lstat(cur)
            except OSError:
                raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

            expected_id = dir_identities.get(cur)
            if expected_id is None or (st_cur.st_dev, st_cur.st_ino) != expected_id:
                raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")
            if _is_any_link(cur):
                raise PbzError("UNSAFE_PATH", "refusing to scan link")
            if not stat.S_ISDIR(st_cur.st_mode):
                raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")

            dirs.sort()
            for d in list(dirs):
                full_d = os.path.join(cur, d)
                try:
                    st_d = os.lstat(full_d)
                except OSError:
                    raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
                if _is_any_link(full_d):
                    raise PbzError("UNSAFE_PATH", "refusing to scan link")
                if not stat.S_ISDIR(st_d.st_mode):
                    raise PbzError("UNSAFE_PATH", "unrecognized file type during scan")
                dir_identities[full_d] = (st_d.st_dev, st_d.st_ino)
                rel_d = os.path.relpath(full_d, root_dir)
                norm_rel_d = _encode_rel_path(rel_d)
                h.update(f"{prefix}:dir:{norm_rel_d}:".encode("utf-8", errors="surrogateescape"))

            files.sort()
            for fname in files:
                fpath = os.path.join(cur, fname)
                rel = os.path.relpath(fpath, root_dir)
                _hash_file(prefix, rel, fpath, root_dir)

        # Post-traversal sweep: re-verify every recorded directory's identity, type, and link status
        for d_path, expected_stat in dir_identities.items():
            try:
                st_sweep = os.lstat(d_path)
            except OSError:
                raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
            if ((st_sweep.st_dev, st_sweep.st_ino) != expected_stat or
                    _is_any_link(d_path) or not stat.S_ISDIR(st_sweep.st_mode)):
                raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")

        # Detect omitted or skipped directories (e.g. followlinks=False skipping linked dirs)
        unvisited = set(dir_identities.keys()) - visited_dirs
        if unvisited:
            raise PbzError("UNSAFE_PATH", "directory not visited during traversal")

        for d_p, obs_id in dir_identities.items():
            prev_d_id = all_dir_identities.setdefault(d_p, obs_id)
            if prev_d_id != obs_id:
                raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")

    # 1. Mirror targets: model and report under project
    has_project_mirror = False
    model_dir = getattr(project, "mirror_model", None)
    if model_dir:
        checked = _inspect_path(model_dir, expect_dir=True, optional=True,
                                stop_dir=abs_ws_root, ancestor_identities=ancestor_identities,
                                absent_ancestors=all_absent_ancestors)
        if checked:
            has_project_mirror = True
            h.update(b"mirror:model:present:\n")
            abs_p, st_info = checked
            _walk_dir("mirror:model", abs_p, st_info)
        else:
            all_absent_paths.add(os.path.abspath(model_dir))
            h.update(b"mirror:model:absent:\n")

    report_dir = getattr(project, "mirror_report", None)
    if report_dir:
        checked = _inspect_path(report_dir, expect_dir=True, optional=True,
                                stop_dir=abs_ws_root, ancestor_identities=ancestor_identities,
                                absent_ancestors=all_absent_ancestors)
        if checked:
            has_project_mirror = True
            h.update(b"mirror:report:present:\n")
            abs_p, st_info = checked
            _walk_dir("mirror:report", abs_p, st_info)
        else:
            all_absent_paths.add(os.path.abspath(report_dir))
            h.update(b"mirror:report:absent:\n")

    if not has_project_mirror:
        ws_mirror = getattr(ws, "mirror", None)
        if ws_mirror:
            checked = _inspect_path(ws_mirror, expect_dir=True, optional=True,
                                    stop_dir=abs_ws_root, ancestor_identities=ancestor_identities,
                                    absent_ancestors=all_absent_ancestors)
            if checked:
                h.update(b"mirror:ws:present:\n")
                abs_p, st_info = checked
                _walk_dir("mirror:ws", abs_p, st_info)
            else:
                all_absent_paths.add(os.path.abspath(ws_mirror))
                h.update(b"mirror:ws:absent:\n")

    # 2. Policy candidates
    policy_path = getattr(ws, "policy_path", None)
    if policy_path:
        checked = _inspect_path(policy_path, expect_dir=False, optional=True,
                                stop_dir=abs_ws_root, ancestor_identities=ancestor_identities,
                                absent_ancestors=all_absent_ancestors)
        if checked:
            h.update(b"policy:ws:present:\n")
            abs_p, st_sig = checked
            _hash_file("policy:ws", os.path.basename(abs_p), abs_p, getattr(ws, "pbz_dir", abs_ws_root),
                       expected_sig=st_sig)
        else:
            all_absent_paths.add(os.path.abspath(policy_path))
            h.update(b"policy:ws:absent:\n")

    root_policy = os.path.join(abs_ws_root, "policy.json")
    if root_policy != policy_path:
        checked = _inspect_path(root_policy, expect_dir=False, optional=True,
                                stop_dir=abs_ws_root, ancestor_identities=ancestor_identities,
                                absent_ancestors=all_absent_ancestors)
        if checked:
            h.update(b"policy:root:present:\n")
            abs_p, st_sig = checked
            _hash_file("policy:root", os.path.basename(abs_p), abs_p, abs_ws_root,
                       expected_sig=st_sig)
        else:
            all_absent_paths.add(os.path.abspath(root_policy))
            h.update(b"policy:root:absent:\n")

    # 3. Rule directories (catalog + workspace rules)
    cat_parent_info: tuple[str, tuple[int, int]] | None = None
    catalog_dir = getattr(bpa, "RULES_DIR", None)
    if catalog_dir:
        abs_cat = os.path.abspath(catalog_dir)
        cat_parent = os.path.dirname(abs_cat)
        if _is_any_link(abs_cat):
            raise PbzError("UNSAFE_PATH", "refusing to scan link")
        if _is_any_link(cat_parent):
            raise PbzError("UNSAFE_PATH", "refusing to scan link")
        try:
            st_cat = os.lstat(cat_parent)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        cat_parent_info = (cat_parent, (st_cat.st_dev, st_cat.st_ino))

        checked = _inspect_path(catalog_dir, expect_dir=True, optional=True, stop_dir=cat_parent,
                                ancestor_identities=ancestor_identities,
                                absent_ancestors=all_absent_ancestors)
        if checked:
            h.update(b"rule:catalog:present:\n")
            abs_p, st_info = checked
            _walk_dir("rule:catalog", abs_p, st_info)
        else:
            all_absent_paths.add(os.path.abspath(catalog_dir))
            h.update(b"rule:catalog:absent:\n")

    ws_rules = os.path.join(abs_ws_root, "rules")
    checked = _inspect_path(ws_rules, expect_dir=True, optional=True,
                            stop_dir=abs_ws_root, ancestor_identities=ancestor_identities,
                            absent_ancestors=all_absent_ancestors)
    if checked:
        h.update(b"rule:workspace:present:\n")
        abs_p, st_info = checked
        _walk_dir("rule:workspace", abs_p, st_info)
    else:
        all_absent_paths.add(os.path.abspath(ws_rules))
        h.update(b"rule:workspace:absent:\n")

    # Re-verify recorded intermediate ancestors
    for anc_path, expected_anc_id in ancestor_identities.items():
        try:
            st_anc_final = os.lstat(anc_path)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        if ((st_anc_final.st_dev, st_anc_final.st_ino) != expected_anc_id or
                _is_any_link(anc_path) or not stat.S_ISDIR(st_anc_final.st_mode)):
            raise PbzError("UNSAFE_PATH", "intermediate ancestor replaced during scan")

    # Re-verify workspace root boundary identity and link-status before returning digest
    try:
        st_ws_final = os.lstat(abs_ws_root)
    except OSError:
        raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
    if ((st_ws_final.st_dev, st_ws_final.st_ino) != ws_initial_id or
            _is_any_link(abs_ws_root) != ws_initial_link or
            not stat.S_ISDIR(st_ws_final.st_mode)):
        raise PbzError("UNSAFE_PATH", "workspace root replaced during scan")

    # Re-verify catalog parent boundary identity if catalog rules were inspected
    if cat_parent_info is not None:
        c_parent, expected_cat_id = cat_parent_info
        try:
            st_cat_final = os.lstat(c_parent)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        if ((st_cat_final.st_dev, st_cat_final.st_ino) != expected_cat_id or
                _is_any_link(c_parent) or not stat.S_ISDIR(st_cat_final.st_mode)):
            raise PbzError("UNSAFE_PATH", "catalog parent replaced during scan")

    # Final whole-scan sweep: re-verify every traversed directory across model, report, and rules
    for d_path, expected_d_id in all_dir_identities.items():
        try:
            st_d_final = os.lstat(d_path)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        if ((st_d_final.st_dev, st_d_final.st_ino) != expected_d_id or
                _is_any_link(d_path) or not stat.S_ISDIR(st_d_final.st_mode)):
            raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")

    # Final whole-scan sweep: re-verify directory membership stability
    for d_path, (expected_dirs, expected_files) in all_dir_entries.items():
        try:
            curr_entries = os.listdir(d_path)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        curr_d = []
        curr_f = []
        for e in curr_entries:
            full_e = os.path.join(d_path, e)
            try:
                st_e = os.lstat(full_e)
                if stat.S_ISDIR(st_e.st_mode):
                    curr_d.append(e)
                else:
                    curr_f.append(e)
            except OSError:
                raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        if tuple(sorted(curr_d)) != expected_dirs or tuple(sorted(curr_f)) != expected_files:
            raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")

    # Final whole-scan sweep: re-verify all scanned files
    for f_path, expected_f_sig in all_file_identities.items():
        try:
            st_f_final = os.lstat(f_path)
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None
        curr_sig = (st_f_final.st_dev, st_f_final.st_ino, st_f_final.st_size, st_f_final.st_mtime_ns)
        if ((curr_sig != expected_f_sig) or
                _is_any_link(f_path) or not stat.S_ISREG(st_f_final.st_mode)):
            raise PbzError("UNSAFE_PATH", "file modified or replaced during scan")

    # Final whole-scan sweep: re-verify absence and non-link status of ancestors recorded as absent
    for anc_p in all_absent_ancestors:
        if _is_any_link(anc_p):
            raise PbzError("UNSAFE_PATH", "refusing to scan link")
        try:
            os.lstat(anc_p)
            raise PbzError("UNSAFE_PATH", "directory modified or replaced during traversal")
        except FileNotFoundError:
            pass
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

    # Final whole-scan sweep: re-verify absence and non-link status of paths recorded as absent
    for abs_p in all_absent_paths:
        if _is_any_link(abs_p):
            raise PbzError("UNSAFE_PATH", "refusing to scan link")
        try:
            os.lstat(abs_p)
            raise PbzError("UNSAFE_PATH", "file modified or replaced during scan")
        except FileNotFoundError:
            pass
        except OSError:
            raise PbzError("SCAN_FAILED", "filesystem scan failed") from None

    return h.hexdigest()


def run(ws, project, once: bool = False, seconds: int = 5,
        rescan: bool = False,
        max_passes: int = 50,
        intervals: int | None = None) -> dict:
    bounded_passes = min(max(int(max_passes), 1), 1000)
    passes: deque[dict] = deque(maxlen=bounded_passes)

    def _once() -> dict:
        findings = validate.run_mirror(ws, project)
        rules = bpa.run(ws, project, "all")
        count = (rules.get("model") or {}).get("findings", 0)
        report = rules.get("report")
        if isinstance(report, dict):
            count += report.get("findings", 0)
        return {"validate": len(findings),
                "validate_errors": sum(1 for f in findings if f.get("severity") == "error"),
                "bpa": count}

    if once:
        # One-shot mode: synchronous, fail-closed, does not swallow KeyboardInterrupt or errors
        d1 = tree_digest(ws, project)
        cand = _once()
        d2 = tree_digest(ws, project)
        if d1 != d2:
            raise PbzError("SCAN_FAILED", "workspace modified during validation pass")
        passes.append(cand)
        return {"verb": "watch", "once": True, "passes": list(passes)}

    # Continuous polling mode
    last_digest: str | None = None
    iter_count = 0
    poll_sec = min(max(int(seconds), 1), 3600)
    consecutive_scan_failures = 0
    MAX_CONSECUTIVE_FAILURES = 10

    while True:
        iter_count += 1
        try:
            curr_digest = tree_digest(ws, project)
        except PbzError as err:
            if err.code == "SCAN_FAILED":
                consecutive_scan_failures += 1
                if consecutive_scan_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise PbzError("SCAN_FAILED", "exceeded maximum consecutive scan failures") from None
                if intervals is not None and iter_count >= intervals:
                    break
                try:
                    time.sleep(poll_sec)
                except KeyboardInterrupt:
                    break
                continue
            raise
        except KeyboardInterrupt:
            break

        try:
            if last_digest is None or curr_digest != last_digest or rescan:
                candidate = _once()
                try:
                    post_digest = tree_digest(ws, project)
                except PbzError as err:
                    if err.code == "SCAN_FAILED":
                        consecutive_scan_failures += 1
                        last_digest = None
                        if consecutive_scan_failures >= MAX_CONSECUTIVE_FAILURES:
                            raise PbzError("SCAN_FAILED", "exceeded maximum consecutive scan failures") from None
                        if intervals is not None and iter_count >= intervals:
                            break
                        try:
                            time.sleep(poll_sec)
                        except KeyboardInterrupt:
                            break
                        continue
                    last_digest = None
                    raise
                if post_digest == curr_digest:
                    passes.append(candidate)
                    last_digest = curr_digest
                    consecutive_scan_failures = 0
                else:
                    # Mismatch: inputs changed during validation pass. Discard candidate and invalidate.
                    last_digest = None
            else:
                # Idle polling interval with identical digest: clean pass, reset failures
                consecutive_scan_failures = 0
        except KeyboardInterrupt:
            break

        if intervals is not None and iter_count >= intervals:
            break
        try:
            time.sleep(poll_sec)
        except KeyboardInterrupt:
            break

    return {"verb": "watch", "once": False, "passes": list(passes)}
