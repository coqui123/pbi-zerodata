"""Read-only diagnostics for OS isolation and CI/CD readiness. Never modifies files or state."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import desktop

ISOLATION = (
    ("account", "Separate standard account for agent sessions"),
    ("ntfs", "NTFS deny ACLs on real model, report, data and private folders"),
    ("broker", "pbz runs as the data owner; the agent sees only closed output"),
    ("network", "Firewall blocks the agent account from Power BI, Fabric and OData"),
    ("disposable", "Windows Sandbox or a VM that holds only the mirror"),
)
DOCTOR_NOTE = "heuristic mode is not OS-isolated; closed verdicts do not prove non-inference."

CLOSED_REASONS = {
    "OFFLINE", "UNAUTHENTICATED", "PERMISSION_DENIED", "NOT_FOUND", "NO_GH", "TIMEOUT", "API_ERROR",
    "DETACHED_HEAD", "NO_BRANCH", "NO_REMOTE", "NOT_RUN", "MISSING_MANIFEST", "PARSE_ERROR",
    "UNEXPECTED_SKIPS", "MISSING_SKIPS", "ABSENT", "UNAVAILABLE", "GIT_UNAVAILABLE", "UNBORN",
    "PAGINATION_INCOMPLETE", "NO_SUCCESSFUL_CHECKS", "STALE_RUN", "PLATFORM_MISMATCH", "BRANCH_MISMATCH",
    "SUITE_FAILED", "RATE_LIMITED", "DIRTY_WORKSPACE", "DIFF_INDEX_FAILED", "NOT_INSTALLED", "FOREIGN_HOOK",
    "HOOK_MODE_FAST", "LOCAL_OPT_IN_PRE_COMMIT_SKIPS_PRE_PUSH_FULL", "AMBIGUOUS_EVIDENCE", "MISSING_SUITES",
    "UNPROTECTED", "CHECKS_FAILED", "PENDING_CHECKS", "REQUIREMENTS_UNKNOWN",
    "NOT_LAUNCHED", "SCAN_FAILED", "RUN_UNBOUND", "RETRIEVAL_UNAVAILABLE",
    "CONFLICTING_EVIDENCE", "LANE_MISMATCH",
}

# Ruleset types that are not a CI gate. Every other type stays active and unproven.
_INACTIVE_RULESET_TYPES = frozenset({
    "creation", "update", "deletion", "non_fast_forward", "required_linear_history",
    "required_signatures", "merge_queue", "required_review_thread_resolution",
    "commit_message_pattern", "commit_author_email_pattern", "committer_email_pattern",
    "branch_name_pattern", "tag_name_pattern", "file_path_restriction",
    "max_file_path_length", "file_extension_restriction", "max_file_size",
})


def cmd_doctor() -> dict:
    return {"verb": "doctor", "isolation": [{"id": i, "title": t, "status": "unverified"} for i, t in ISOLATION], "note": DOCTOR_NOTE}


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run_git(args: list[str], cwd: Path | None = None, timeout: int = 5) -> tuple[int, str]:
    """Text git output. Bytes are decoded here so a CR in a path is not turned into LF."""
    rc, raw = _run_git_raw(args, cwd=cwd, timeout=timeout)
    if raw is None:
        return -1, ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return -1, ""
    if text.endswith("\n"):
        text = text[:-1]
    return rc, text


def _run_git_raw(args: list[str], cwd: Path | None = None, timeout: int = 5) -> tuple[int, bytes | None]:
    """Bytes of one git command. Callers that must not substitute paths decode UTF-8 themselves."""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "--no-replace-objects"] + args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            shell=False,
            env=_git_env(),
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return -1, None
    return proc.returncode, proc.stdout


def _utf8_z(data: bytes | None) -> str | None:
    """Strict UTF-8. A bad byte is a failure, not a replacement path. NUL stays."""
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.strip()


def _git_text_line(data: bytes | None) -> str | None:
    """One git line. Drop one terminating LF. A trailing CR stays part of the path."""
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.endswith("\n"):
        text = text[:-1]
    return text


def _root_collapsed(original: Path, resolved: Path) -> bool:
    """True when resolving drops surrounding whitespace onto a different directory."""
    def logical(path: Path) -> list[str]:
        out = []
        for part in path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if len(out) > 1 or (len(out) == 1 and out[0] not in ("/", "\\") and not out[0].endswith(":\\") and not out[0].endswith(":")):
                    out.pop()
                continue
            out.append(part)
        return out

    def fold(part: str) -> str:
        return part.casefold() if os.name == "nt" else part

    orig = logical(original)
    got = logical(resolved)
    if len(orig) != len(got):
        return any(part != part.strip() for part in orig)
    for part, have in zip(orig, got):
        if fold(part) == fold(have):
            continue
        stripped = part.strip()
        if stripped != part and fold(stripped) == fold(have):
            return True
    return False


def _find_repo_root(start_dir: Path | None = None) -> Path | None:
    rc, raw = _run_git_raw(["rev-parse", "--show-toplevel"], cwd=start_dir)
    if rc != 0:
        return None
    out = _git_text_line(raw)
    if not out:
        return None
    p = Path(out)
    try:
        resolved = p.resolve()
    except OSError:
        return None
    if _root_collapsed(p, resolved) or not resolved.is_dir():
        return None
    return resolved


def get_head_sha(repo_root: Path | None) -> tuple[str | None, str | None]:
    if not repo_root:
        return None, "GIT_UNAVAILABLE"
    rc, out = _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=repo_root)
    if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", out):
        return out, None
    if rc == 1 and not out:
        return None, "UNBORN"
    return None, "GIT_UNAVAILABLE"


def get_dirty_status(repo_root: Path | None, _seen: set[Path] | None = None) -> tuple[bool | None, str | None]:
    if not repo_root:
        return None, "GIT_UNAVAILABLE"
    seen = set(_seen or ())
    try:
        key = repo_root.resolve()
    except OSError:
        return None, "DIFF_INDEX_FAILED"
    if key in seen:
        return None, "DIFF_INDEX_FAILED"
    seen.add(key)
    rc, out = _run_git(["status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none"], cwd=repo_root)
    if rc != 0:
        return None, "DIFF_INDEX_FAILED"
    if out.strip():
        return True, "DIRTY_WORKSPACE"
    return _hidden_index_edits(repo_root, seen)


def _rel_in_repo(rel: str) -> bool:
    if not rel or rel.startswith(("/", "\\")) or "\n" in rel or "\r" in rel:
        return False
    path = Path(rel)
    if path.is_absolute() or path.drive:
        return False
    return all(part not in ("", "..") for part in path.parts)


def _sparse_checkout(repo_root: Path) -> bool | None:
    rc, out = _run_git(["config", "--local", "--get", "--type=bool", "core.sparseCheckout"], cwd=repo_root)
    if rc == 1 and not out:
        return False
    if rc == 0 and out in ("true", "false"):
        return out == "true"
    return None


def _config_bool(repo_root: Path, key: str, default: bool) -> bool | None:
    rc, out = _run_git(["config", "--get", "--type=bool", key], cwd=repo_root)
    if rc == 0 and out in ("true", "false"):
        return out == "true"
    if rc == 1 and not out:
        return default
    return None


def _parse_stage(out: str) -> dict[str, tuple[str, str]] | None:
    rows: dict[str, tuple[str, str]] = {}
    for rec in out.split("\0"):
        if not rec:
            continue
        if "\t" not in rec:
            return None
        meta, rel = rec.split("\t", 1)
        parts = meta.split(" ")
        if len(parts) != 3:
            return None
        mode, sha, stage = parts
        if stage != "0" or not re.fullmatch(r"[0-7]{6}", mode) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None
        if not _rel_in_repo(rel) or rel in rows:
            return None
        rows[rel] = (mode, sha)
    return rows


def _parse_tags(out: str) -> dict[str, str] | None:
    tags: dict[str, str] = {}
    for rec in out.split("\0"):
        if not rec:
            continue
        if len(rec) < 3 or rec[1] != " ":
            return None
        rel = rec[2:]
        if not _rel_in_repo(rel) or rel in tags:
            return None
        tags[rel] = rec[0]
    return tags


def _hash_stdin(repo_root: Path, data: bytes) -> str | None:
    env = _git_env()
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "hash-object", "--stdin"],
            cwd=str(repo_root),
            input=data,
            capture_output=True,
            timeout=5,
            shell=False,
            env=env,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    work = proc.stdout.decode("ascii", "replace").strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", work):
        return None
    return work


def _worktree_blob(repo_root: Path, rel: str, mode: str, is_link: bool) -> str | None:
    if mode == "120000" and is_link:
        try:
            target = os.readlink(repo_root / rel)
        except OSError:
            return None
        return _hash_stdin(repo_root, os.fsencode(target))
    rc, work = _run_git(["hash-object", f"--path={rel}", "--", rel], cwd=repo_root)
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", work):
        return None
    return work


def _flagged_worktree(
    repo_root: Path,
    rel: str,
    mode: str,
    sha: str,
    assume: bool,
    skip: bool,
    filemode: bool,
    symlinks: bool,
) -> tuple[bool | None, str | None]:
    wt = repo_root / rel
    try:
        is_link = wt.is_symlink()
        is_file = wt.is_file()
    except OSError:
        return None, "DIFF_INDEX_FAILED"
    if is_link and mode != "120000":
        return True, "DIRTY_WORKSPACE"
    if symlinks and mode == "120000" and not is_link:
        return True, "DIRTY_WORKSPACE"
    if not is_file and not is_link:
        if skip and not assume:
            sparse = _sparse_checkout(repo_root)
            if sparse is None:
                return None, "DIFF_INDEX_FAILED"
            if sparse:
                return False, None
        return True, "DIRTY_WORKSPACE"
    if filemode and mode in ("100644", "100755") and not is_link:
        try:
            exec_bit = bool(wt.lstat().st_mode & 0o100)
        except OSError:
            return None, "DIFF_INDEX_FAILED"
        if exec_bit != (mode == "100755"):
            return True, "DIRTY_WORKSPACE"
    work = _worktree_blob(repo_root, rel, mode, is_link)
    if work is None:
        return None, "DIFF_INDEX_FAILED"
    if work != sha:
        return True, "DIRTY_WORKSPACE"
    return False, None


def _hidden_index_edits(repo_root: Path, seen: set[Path]) -> tuple[bool | None, str | None]:
    """Status omits assume-unchanged and skip-worktree edits. Compare blobs. Do not write the index."""
    rc_s, stage_raw = _run_git_raw(["ls-files", "-s", "-z"], cwd=repo_root)
    rc_v, tag_raw = _run_git_raw(["ls-files", "-v", "-z"], cwd=repo_root)
    if rc_s != 0 or rc_v != 0:
        return None, "DIFF_INDEX_FAILED"
    stage_out = _utf8_z(stage_raw)
    tag_out = _utf8_z(tag_raw)
    if stage_out is None or tag_out is None:
        return None, "DIFF_INDEX_FAILED"
    stage = _parse_stage(stage_out)
    tags = _parse_tags(tag_out)
    if stage is None or tags is None:
        return None, "DIFF_INDEX_FAILED"
    flagged = []
    for rel, tag in tags.items():
        assume = tag.isascii() and tag.isalpha() and tag.islower()
        skip = tag in ("S", "s")
        if not assume and not skip:
            continue
        info = stage.get(rel)
        if info is None:
            return None, "DIFF_INDEX_FAILED"
        mode, sha = info
        if mode == "160000":
            continue
        flagged.append((rel, mode, sha, assume, skip))
    filemode = False
    symlinks = False
    if flagged:
        default = sys.platform != "win32"
        filemode = _config_bool(repo_root, "core.filemode", default)
        symlinks = _config_bool(repo_root, "core.symlinks", default)
        if filemode is None or symlinks is None:
            return None, "DIFF_INDEX_FAILED"
    for rel, mode, sha, assume, skip in flagged:
        verdict = _flagged_worktree(repo_root, rel, mode, sha, assume, skip, filemode, symlinks)
        if verdict != (False, None):
            return verdict
    for rel, (mode, sha) in stage.items():
        if mode != "160000":
            continue
        wt = repo_root / rel
        try:
            if wt.is_symlink():
                return True, "DIRTY_WORKSPACE"
            is_dir = wt.is_dir()
            exists = wt.exists()
        except OSError:
            return None, "DIFF_INDEX_FAILED"
        if not is_dir:
            if exists:
                return None, "DIFF_INDEX_FAILED"
            continue
        rc_h, head = _run_git(["rev-parse", "--verify", "-q", "HEAD"], cwd=wt)
        if rc_h != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
            return None, "DIFF_INDEX_FAILED"
        if head != sha:
            return True, "DIRTY_WORKSPACE"
        dirty, reason = get_dirty_status(wt, seen)
        if dirty is True:
            return True, "DIRTY_WORKSPACE"
        if dirty is None:
            return None, reason or "DIFF_INDEX_FAILED"
    return False, None


_DIRECT_HOOK_HASHES = {
    "111c8b7f673d0aa0de4720a111632838d609ea71f5a30cc838ee2e974e6d569a", "3692298c54fb8de3ab867dbde0d45375d0b8611ac9049c3c40c3c5b06882553c",
    "5ae2b12c872881f0dc7e0eba43efb7f6afffb8ea189d4401faf6f0344baa057a", "8e473fe563a3328033763cdf5b5670a557746765aa8a21fae3eea62e1a790523",
    "d6245989a8964efcf6b1cc9e8eaf31ef0521ab506a55ac1781b00fa9db169410",
    "ee4de5dcf8d7fe076883ca2d255c45b5b033a1b4ca06d19b00275446042cf037",
    "a427efa6804910f486e6446dbc4b74752529acb63bd2a1abc9f0b9e84e9381e2", "af7031fcfdb49ac6b76276abfbbbc2c38567bcee84cb96de4416f9f147e6e176",
    "f31b8114f3a1e1c3964fc06e5c4b0917828dc2ed732d6dcbb51ffc33491443b3", "fa59be176f7bfacd06f7565e2e6762913c4b68d0ec580748308418c55a8e96ce",
}
_DELEGATING_HOOK_HASHES = {
    "312d56134a146e8dc46c411e9dd66ca240aa853672e578b566783880ab980c16", "43d0e2266a64f8339fafc441a38a1b4789d66144857a29aecdd9466ba7a300ea",
    "4f96b455129a695ac3ac0d2bf26990aac099698083b7e0276eba434ccf0a228d", "e503b65ec477907aa9c2cea047e526bed523e5388768386bebe686d521c9154b",
}
_CANONICAL_HOOK_HASHES = _DIRECT_HOOK_HASHES | _DELEGATING_HOOK_HASHES


def _check_hook_content(data: bytes) -> str | None:
    """Authenticates byte-exact hook templates against canonical allowlists.
    Returns 'direct', 'delegating', or None if unrecognized.
    Strictly verifies byte-level authenticity without lossy string transformations.
    """
    if not data or not data.startswith(b"#!"):
        return None
    h = hashlib.sha256(data).hexdigest()
    if h in _DIRECT_HOOK_HASHES:
        return "direct"
    if h in _DELEGATING_HOOK_HASHES:
        return "delegating"
    return None


def _is_managed_hook(data: bytes) -> bool:
    return _check_hook_content(data) is not None


def get_hook_mode(repo_root: Path | None) -> str:
    if not repo_root:
        return "unknown"
    rc, git_dir_out = _run_git(["rev-parse", "--git-path", "hooks/pre-push"], cwd=repo_root)
    if rc != 0 or not git_dir_out:
        return "unknown"
    hook_file = (repo_root / git_dir_out).resolve()

    if not hook_file.is_file():
        return "not_installed"
    try:
        hook_bytes = hook_file.read_bytes()
    except OSError:
        return "unknown"

    # Reject non-executable hook on POSIX
    if sys.platform != "win32" and not os.access(hook_file, os.X_OK):
        return "foreign"

    hook_type = _check_hook_content(hook_bytes)
    if not hook_type:
        return "foreign"

    if hook_type == "delegating":
        # Transitively validate the delegated target script
        target_file = (repo_root / "scripts" / "git-hooks" / "pre-push").resolve()
        if not target_file.is_file():
            return "foreign"
        if sys.platform != "win32" and not os.access(target_file, os.X_OK):
            return "foreign"
        try:
            target_bytes = target_file.read_bytes()
        except OSError:
            return "unknown"
        if _check_hook_content(target_bytes) != "direct":
            return "foreign"

    rc, cfg = _run_git(["config", "--get", "pbz.requiredCI"], cwd=repo_root)
    if rc == 0:
        raw = cfg.strip()
        if not raw:
            return "unknown"
        # Exact installer value. Pre-commit skips the suite. Pre-push stays advisory.
        # This is not branch protection and it is not the full gate.
        if raw == "main-offline-required":
            return "main-offline-required"
        val = raw.lower()
        if val in ("fast", "full"):
            return val
        return "unknown"
    elif rc == 1:
        return "full"
    return "unknown"


def _clean_repo_slug(url: str) -> str | None:
    if not url:
        return None
    # Strictly require github.com host and valid owner/repo segments (allowing dots in repo name)
    m = re.match(r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    if not m:
        return None
    slug = m.group(1)
    parts = slug.split("/")
    if any(p in (".", "..") for p in parts):
        return None
    return slug


_UNBOUND_SLUG = object()


def get_origin(repo_root: Path | None) -> tuple[str | None, str | None]:
    """One origin read. The slug and the fetch URL come from that single line."""
    if not repo_root:
        return None, None
    rc, out = _run_git(["remote", "get-url", "origin"], cwd=repo_root)
    if rc != 0 or not out or out != out.strip():
        return None, None
    slug = _clean_repo_slug(out)
    if not slug:
        return None, None
    return slug, out


def get_repo_slug(repo_root: Path | None) -> str | None:
    slug, _url = get_origin(repo_root)
    return slug


def get_current_branch(repo_root: Path | None) -> tuple[str | None, str | None]:
    if not repo_root:
        return None, "NO_BRANCH"
    rc, out = _run_git(["symbolic-ref", "-q", "HEAD"], cwd=repo_root)
    prefix = "refs/heads/"
    if rc == 0 and out.startswith(prefix) and len(out) > len(prefix):
        return out[len(prefix):], None
    rc_rev, out_rev = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    if rc_rev == 0 and out_rev == "HEAD":
        return None, "DETACHED_HEAD"
    return None, "NO_BRANCH"


def _json_object(pairs: list) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _parse_api_json(raw: str | bytes) -> tuple[int, dict | list, str | None]:
    if not raw or not (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw).strip():
        return -1, {}, "API_ERROR"
    try:
        data = json.loads(raw, object_pairs_hook=_json_object)
        if not isinstance(data, (dict, list)):
            return -1, {}, "API_ERROR"
        return 200, data, None
    except (ValueError, TypeError):
        return -1, {}, "API_ERROR"


def _api_get(slug: str | None, path: str, cwd: Path | None = None, timeout: int = 5) -> tuple[int, dict | list, str | None]:
    if not slug:
        return -1, {}, "NO_REMOTE"

    def _err_reason(code: int, msg: str) -> tuple[int, str]:
        m = " ".join(msg.lower().split())
        if code == 404 and m in {
            "branch not protected",
            "http 404: branch not protected",
            "gh: branch not protected (http 404)",
        }:
            return 404, "UNPROTECTED"
        if code == 404 or "not found" in m:
            return 404, "NOT_FOUND"
        if "rate limit" in m or code == 429:
            return 403, "RATE_LIMITED"
        if any(net in m for net in ("could not resolve", "failed to connect", "connection refused", "network is unreachable")):
            return -1, "OFFLINE"
        if code in (401, 403) or any(k in m for k in ("bad credentials", "unauthorized", "permission")):
            return code or 401, "UNAUTHENTICATED" if (code == 401 or "credentials" in m) else "PERMISSION_DENIED"
        return code or -1, "API_ERROR"

    gh_bin, token = shutil.which("gh"), os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_bin:
        try:
            cmd = [gh_bin, "api", "--hostname", "github.com", f"repos/{slug}/{path}", "--header", "Accept: application/vnd.github+json"]
            proc = subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                timeout=timeout,
            )
            try:
                stdout = proc.stdout.decode("utf-8") if isinstance(proc.stdout, bytes) else (proc.stdout or "")
                stderr = proc.stderr.decode("utf-8") if isinstance(proc.stderr, bytes) else (proc.stderr or "")
            except UnicodeDecodeError:
                return -1, {}, "API_ERROR"
            if proc.returncode == 0:
                return _parse_api_json(stdout)
            m_http = re.search(r"HTTP\s+(\d{3})", stderr)
            http_code = int(m_http.group(1)) if m_http else proc.returncode
            if stdout and stdout.strip():
                parsed_code, parsed_data, parsed_reason = _parse_api_json(stdout)
                if parsed_reason == "API_ERROR" or parsed_code != 200:
                    return http_code or -1, {}, "API_ERROR"
                raw_msg = parsed_data.get("message") if isinstance(parsed_data, dict) else None
                msg = raw_msg if isinstance(raw_msg, str) else ""
                c, rsn = _err_reason(http_code, msg)
                return c, {}, rsn
            c, rsn = _err_reason(http_code, stderr)
            return c, {}, rsn
        except subprocess.TimeoutExpired:
            return -1, {}, "TIMEOUT"
        except (subprocess.SubprocessError, OSError, ValueError, UnicodeDecodeError):
            return -1, {}, "API_ERROR"

    if token:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{slug}/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "pbz-doctor-ci"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _parse_api_json(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                parsed = json.loads(e.read().decode("utf-8"), object_pairs_hook=_json_object)
                raw_msg = parsed.get("message") if isinstance(parsed, dict) else None
                msg = raw_msg if isinstance(raw_msg, str) else ""
            except Exception:
                msg = ""
            c, rsn = _err_reason(e.code, msg)
            return c, {}, rsn
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException):
            return -1, {}, "OFFLINE"
        except (ValueError, OSError):
            return -1, {}, "API_ERROR"

    return -1, {}, "NO_GH"


def _context_proven(checks, ctx_name: str, expected_app) -> bool:
    """True only when every matching run completed with success.

    One success does not hide a skipped, neutral, failed, or unfinished run
    of the same context. expected_app None matches every app.
    """
    matched = []
    for c in checks:
        if not isinstance(c, dict) or c.get("name") != ctx_name:
            continue
        if expected_app is not None and c.get("app_id") != expected_app:
            continue
        matched.append(c)
    if not matched:
        return False
    return all(
        c.get("status") == "completed" and c.get("conclusion") == "success"
        for c in matched
    )


def _latest_commit_statuses(slug: str, sha: str, cwd: Path | None, timeout: int) -> tuple[dict[str, str] | None, str | None]:
    """Newest commit status per context. None means the list could not be proven."""
    st_code, st_data, st_reason = _api_get(slug, f"commits/{sha}/statuses?per_page=100", cwd=cwd, timeout=timeout)
    if st_code != 200 or not isinstance(st_data, list):
        return None, "API_ERROR" if st_code == 200 else (st_reason or "API_ERROR")
    if len(st_data) >= 100:
        return None, "PAGINATION_INCOMPLETE"
    latest: dict[str, str] = {}
    for item in st_data:
        if not isinstance(item, dict):
            return None, "API_ERROR"
        ctx, state, row_sha = item.get("context"), item.get("state"), item.get("sha")
        if (
            not isinstance(ctx, str) or not ctx.strip()
            or not isinstance(state, str) or not state
            or not isinstance(row_sha, str) or row_sha != sha
        ):
            return None, "API_ERROR"
        key = ctx.casefold()
        if key not in latest:
            latest[key] = state
    return latest, None


def get_hosted_checks(
    repo_root: Path | None,
    sha: str | None,
    offline: bool = False,
    timeout: int = 5,
    required_contexts: list[str] | None = None,
    required_checks: list[dict] | None = None,
    origin_slug=_UNBOUND_SLUG,
) -> dict:
    def _res(st, rsn, chks=None, c_sha=None):
        return {"checked_sha": c_sha, "status": st, "reason": rsn, "checks": chks or []}

    if offline:
        return _res("unknown", "OFFLINE")
    if not sha:
        return _res("unknown", "UNAVAILABLE")

    if origin_slug is _UNBOUND_SLUG:
        slug = get_repo_slug(repo_root)
    else:
        slug = origin_slug
    if not slug:
        return _res("unknown", "NO_REMOTE")

    status_code, data, reason = _api_get(slug, f"commits/{sha}/check-runs?per_page=100&filter=all", cwd=repo_root, timeout=timeout)
    if status_code == 200:
        if not isinstance(data, dict):
            return _res("unknown", "API_ERROR", chks=[], c_sha=None)
        runs = data.get("check_runs")
        total_count = data.get("total_count")
        if not isinstance(runs, list) or isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            return _res("unknown", "API_ERROR", chks=[], c_sha=None)
        if total_count == 0 and not runs:
            return _res("absent", "NOT_FOUND", chks=[], c_sha=sha)
        if total_count > len(runs):
            return _res("unknown", "PAGINATION_INCOMPLETE", chks=[], c_sha=sha)
        if total_count < len(runs):
            return _res("unknown", "API_ERROR", chks=[], c_sha=None)

        checks, has_failure, has_pending, success_count = [], False, False, 0
        for r in runs:
            if not isinstance(r, dict) or r.get("head_sha") != sha or not isinstance(r.get("head_sha"), str):
                return _res("unknown", "API_ERROR", chks=[], c_sha=None)
            run_name = r.get("name")
            if not isinstance(run_name, str) or not run_name.strip():
                return _res("unknown", "API_ERROR", chks=[], c_sha=None)
            st, concl = r.get("status"), r.get("conclusion")
            app_dict = r.get("app") if isinstance(r.get("app"), dict) else {}
            raw_id = app_dict.get("id")
            app_id = raw_id if (isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0) else None
            checks.append({
                "name": run_name,
                "status": str(st),
                "conclusion": str(concl) if concl else None,
                "app_id": app_id,
            })
            if concl in ("failure", "timed_out", "action_required", "cancelled", "startup_failure"):
                has_failure = True
            elif st != "completed" or concl is None:
                has_pending = True
            elif concl == "success":
                success_count += 1
            elif concl not in ("neutral", "skipped"):
                has_pending = True

        # Check names must match trusted CI/test patterns and app MUST be github-actions
        def _is_ci_check_name(name: str, app: dict | None) -> bool:
            if not (isinstance(app, dict) and app.get("slug") == "github-actions"):
                return False
            tokens = set(re.split(r"[\s/_.:-]+", name.strip().lower()))
            return bool(tokens & {"test", "tests", "ci", "offline", "serial", "shard"})

        has_ci_skipped_or_neutral = any(
            r.get("conclusion") in ("skipped", "neutral") and _is_ci_check_name(str(r.get("name", "")), r.get("app"))
            for r in runs
        )

        # Check required checks and contexts if specified by branch protection
        if required_checks:
            req_source = required_checks
        else:
            req_source = [{"context": c, "app_id": None} for c in (required_contexts or []) if isinstance(c, str)]
        req_list = [
            r for r in req_source
            if isinstance(r, dict) and isinstance(r.get("context"), str)
        ]
        if required_checks and len(req_list) != len(required_checks):
            return _res("unknown", "API_ERROR", chks=checks, c_sha=sha)
        latest_status: dict[str, str] = {}
        if req_list:
            latest_status_or_none, status_reason = _latest_commit_statuses(slug, sha, repo_root, timeout)
            if latest_status_or_none is None:
                return _res("unknown", status_reason or "API_ERROR", chks=checks, c_sha=sha)
            latest_status = latest_status_or_none

        has_missing_req = False
        for req in req_list:
            ctx_name = req["context"]
            expected_app_id = req.get("app_id")
            state = latest_status.get(ctx_name.casefold())
            if req.get("kind") in ("workflow", "unproven"):
                satisfied = False
            elif state in ("failure", "error"):
                has_failure = True
                satisfied = False
            elif state not in (None, "success"):
                has_pending = True
                satisfied = False
            else:
                satisfied = _context_proven(checks, ctx_name, expected_app_id)
                if satisfied and expected_app_id is not None:
                    satisfied = _context_proven(checks, ctx_name, None)
            if not satisfied:
                has_missing_req = True
                break

        # If required checks are specified and satisfied, authoritatively satisfied; else check name heuristic
        has_ci_success = (
            (bool(req_list) and not has_missing_req)
            or any(
                r.get("status") == "completed"
                and r.get("conclusion") == "success"
                and _is_ci_check_name(str(r.get("name", "")), r.get("app"))
                for r in runs
            )
        )

        # A satisfied required list is the authority. An optional skipped job
        # must not force pending. With no required list, a skipped CI-named job still does.
        required_satisfied = bool(req_list) and not has_missing_req
        optional_skip_blocks = has_ci_skipped_or_neutral and not required_satisfied
        if has_failure:
            overall, rsn = "failure", "CHECKS_FAILED"
        elif has_pending or optional_skip_blocks or has_missing_req:
            overall, rsn = "pending", "PENDING_CHECKS"
        elif success_count > 0 and has_ci_success:
            overall, rsn = "success", None
        else:
            return _res("unknown", "NO_SUCCESSFUL_CHECKS", chks=checks, c_sha=sha)

        return _res(overall, rsn, chks=checks, c_sha=sha)

    return _res("absent" if reason == "NOT_FOUND" else "unknown", reason, chks=[], c_sha=None)


def _validate_classic_protection(data: dict) -> tuple[bool, bool, list[dict]]:
    """Validates classic branch protection schema.
    Returns (is_valid_schema, is_actively_protected, required_checks).
    Strictly validates types and requires active status checks, reviews, or admin enforcement.
    """
    if not isinstance(data, dict):
        return False, False, []
    url = data.get("url")
    if not (isinstance(url, str) and url.startswith("https://") and len(url) > 10):
        return False, False, []

    rsc, ea, rpr = data.get("required_status_checks"), data.get("enforce_admins"), data.get("required_pull_request_reviews")

    active_rsc = False
    req_checks: list[dict] = []
    if rsc is not None:
        if not isinstance(rsc, dict) or ("strict" in rsc and not isinstance(rsc["strict"], bool)):
            return False, False, []
        ctx, chk = rsc.get("contexts"), rsc.get("checks")
        if "contexts" in rsc and not isinstance(ctx, list):
            return False, False, []
        if "checks" in rsc and not isinstance(chk, list):
            return False, False, []
        if ctx is None and chk is None:
            return False, False, []
        if ctx is not None:
            if not isinstance(ctx, list) or not all(isinstance(c, str) and c.strip() for c in ctx):
                return False, False, []
            active_rsc = active_rsc or bool(ctx)
            for c in ctx:
                if isinstance(c, str) and c.strip():
                    req_checks.append({"context": c, "app_id": None})
        if chk is not None:
            if not isinstance(chk, list):
                return False, False, []
            for c in chk:
                if not (isinstance(c, dict) and isinstance(c.get("context"), str) and c.get("context").strip()):
                    return False, False, []
                app_id = c.get("app_id")
                if isinstance(app_id, bool) or (app_id is not None and not isinstance(app_id, int)):
                    return False, False, []
                if app_id == -1:
                    app_id = None
                elif app_id is not None and app_id <= 0:
                    return False, False, []
                req_checks.append({"context": c["context"], "app_id": app_id})
            active_rsc = active_rsc or bool(chk)

    if ea is not None:
        if not isinstance(ea, dict) or not isinstance(ea.get("enabled"), bool):
            return False, False, []

    active_rpr = False
    if rpr is not None:
        if not isinstance(rpr, dict) or not rpr:
            return False, False, []
        recognized_keys = {
            "required_approving_review_count",
            "dismiss_stale_reviews",
            "require_code_owner_reviews",
            "require_last_push_approval",
            "dismissal_restrictions",
            "bypass_pull_request_allowances",
            "url",
        }
        if not (set(rpr.keys()) & recognized_keys):
            return False, False, []
        if "url" in rpr:
            u = rpr.get("url")
            if not (isinstance(u, str) and u.startswith("https://")):
                return False, False, []
        if "required_approving_review_count" in rpr:
            cnt = rpr.get("required_approving_review_count")
            if isinstance(cnt, bool) or not isinstance(cnt, int) or cnt < 0:
                return False, False, []
        for key in ("dismiss_stale_reviews", "require_code_owner_reviews", "require_last_push_approval"):
            if key in rpr and not isinstance(rpr.get(key), bool):
                return False, False, []
        for key in ("dismissal_restrictions", "bypass_pull_request_allowances"):
            if key in rpr and not isinstance(rpr.get(key), dict):
                return False, False, []
        active_rpr = True

    is_actively_protected = active_rsc or active_rpr
    return True, is_actively_protected, req_checks


def get_branch_protection(repo_root: Path | None, branch: str | None, branch_reason: str | None = None, offline: bool = False, timeout: int = 5, origin_slug=_UNBOUND_SLUG) -> dict:
    def _res(st, rsn, rset=None, chks=None):
        req_checks = chks or []
        ctxs = sorted(list({c["context"] for c in req_checks if isinstance(c, dict) and "context" in c}))
        return {
            "branch": branch,
            "status": st,
            "reason": rsn,
            "ruleset": rset,
            "required_contexts": ctxs,
            "required_checks": req_checks,
        }

    if not branch:
        return _res("unknown", branch_reason or "NO_BRANCH")
    if offline:
        return _res("unknown", "OFFLINE")

    if origin_slug is _UNBOUND_SLUG:
        slug = get_repo_slug(repo_root)
    else:
        slug = origin_slug
    if not slug:
        return _res("unknown", "NO_REMOTE")

    encoded = urllib.parse.quote(branch, safe="")

    # 1. Query classic protection
    c_status, c_data, c_reason = _api_get(slug, f"branches/{encoded}/protection", cwd=repo_root, timeout=timeout)
    protected_classic = False
    classic_checks: list[dict] = []
    classic_unprotected = False

    if c_status == 200:
        valid_c, protected_classic, classic_checks = _validate_classic_protection(c_data)
        if not valid_c:
            return _res("unknown", "API_ERROR")
        if not protected_classic:
            classic_unprotected = True
    elif c_reason == "UNPROTECTED":
        classic_unprotected = True
    else:
        # Generic 404, 403, rate limit, timeout, or API error makes classic unreadable
        classic_unprotected = False

    # 2. Query repository rulesets
    r_code, r_data, r_reason = _api_get(slug, f"rules/branches/{encoded}?per_page=100", cwd=repo_root, timeout=timeout)
    protected_ruleset = False
    ruleset_checks: list[dict] = []
    ruleset_unprotected = False

    if r_code == 200 and isinstance(r_data, list):
        if len(r_data) >= 100:
            return _res("unknown", "PAGINATION_INCOMPLETE")

        def _validate_ruleset_rule(r: dict) -> tuple[bool, bool, list[dict]]:
            if not (
                isinstance(r, dict)
                and not isinstance(r.get("ruleset_id"), bool)
                and isinstance(r.get("ruleset_id"), int)
                and r.get("ruleset_id") > 0
                and isinstance(r.get("type"), str)
                and bool(r.get("type").strip())
            ):
                return False, False, []
            rtype = r.get("type")
            if rtype == "pull_request":
                return True, True, []
            if rtype == "required_status_checks":
                params = r.get("parameters")
                if not isinstance(params, dict):
                    return False, False, []
                chks = params.get("required_status_checks")
                if not isinstance(chks, list) or not chks:
                    return False, False, []
                ruleset_chks = []
                for c in chks:
                    if not (isinstance(c, dict) and isinstance(c.get("context"), str) and bool(c.get("context").strip())):
                        return False, False, []
                    int_id = c.get("integration_id")
                    if isinstance(int_id, bool) or (int_id is not None and not isinstance(int_id, int)):
                        return False, False, []
                    if int_id == -1:
                        int_id = None
                    elif int_id is not None and int_id <= 0:
                        return False, False, []
                    ruleset_chks.append({"context": c["context"], "app_id": int_id})
                return True, True, ruleset_chks
            if rtype == "workflows":
                params = r.get("parameters")
                if not isinstance(params, dict):
                    return False, False, []
                flows = params.get("workflows")
                if not isinstance(flows, list) or not flows:
                    return False, False, []
                parsed = []
                for flow in flows:
                    if not isinstance(flow, dict):
                        return False, False, []
                    path = flow.get("path")
                    if not isinstance(path, str) or not path.strip():
                        return False, False, []
                    ref = flow.get("ref")
                    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
                        return False, False, []
                    repo_id = flow.get("repository_id")
                    if repo_id is not None and (isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id <= 0):
                        return False, False, []
                    parsed.append({
                        "context": path.strip(),
                        "app_id": None,
                        "kind": "workflow",
                        "ref": ref.strip() if isinstance(ref, str) else None,
                        "repository_id": repo_id if isinstance(repo_id, int) else None,
                    })
                return True, True, parsed
            if rtype in _INACTIVE_RULESET_TYPES:
                return True, False, []
            return True, True, [{"context": rtype, "app_id": None, "kind": "unproven"}]

        rule_results = [_validate_ruleset_rule(r) for r in r_data]
        if not all(valid for valid, _, _ in rule_results):
            return _res("unknown", "API_ERROR")

        protected_ruleset = any(active for _, active, _ in rule_results)
        for _, _, chk_list in rule_results:
            ruleset_checks.extend(chk_list)
        if not protected_ruleset:
            ruleset_unprotected = True
    elif r_code == 200 and not isinstance(r_data, list):
        return _res("unknown", "API_ERROR")

    # Incomplete policy visibility gate: if either source is unreadable, fail closed to unknown!
    classic_readable = (c_status == 200 or classic_unprotected)
    ruleset_readable = (r_code == 200)

    if not classic_readable or not ruleset_readable:
        fail_reason = c_reason if not classic_readable else r_reason
        return _res("unknown", fail_reason or "API_ERROR")

    # Both sources are readable: combine checks deduplicating by (context, app_id)
    def _plain_checks(checks):
        seen = {}
        for chk in checks:
            if chk.get("kind") in ("workflow", "unproven"):
                continue
            seen[(chk["context"], chk.get("app_id"))] = chk
        specific = {ctx for (ctx, app_id) in seen if app_id is not None}
        return [
            chk for (ctx, app_id), chk in seen.items()
            if not (app_id is None and ctx in specific)
        ]

    sticky = [
        chk for chk in classic_checks + ruleset_checks
        if chk.get("kind") in ("workflow", "unproven")
    ]
    seen_checks = {}
    for chk in _plain_checks(classic_checks) + _plain_checks(ruleset_checks):
        seen_checks[(chk["context"], chk.get("app_id"))] = chk
    final_checks = list(seen_checks.values())
    final_checks.extend(sticky)

    if protected_classic and protected_ruleset:
        return _res("protected", None, rset="ruleset+classic", chks=final_checks)
    if protected_classic:
        return _res("protected", None, rset="classic", chks=final_checks)
    if protected_ruleset:
        return _res("protected", None, rset="ruleset", chks=final_checks)

    if classic_unprotected and ruleset_unprotected:
        return _res("unprotected", "UNPROTECTED")

    return _res("unknown", "NOT_FOUND")


def _parse_skip_records(lines: list) -> tuple[set[str] | None, bool]:
    out = set()
    for line in lines:
        if not isinstance(line, str):
            return None, False
        s = line.strip()
        if not s:
            continue
        if s.startswith("PBZ_SKIP "):
            s = s[len("PBZ_SKIP "):].strip()
        try:
            d = json.loads(s, object_pairs_hook=_json_object)
            if isinstance(d, dict) and "test" in d and isinstance(d["test"], str) and "reason" in d and isinstance(d["reason"], str):
                out.add(json.dumps({"test": d["test"], "reason": d["reason"]}, sort_keys=True))
            else:
                return None, False
        except (ValueError, TypeError):
            return None, False
    return out, True


def _serial_skip_status(serial_file: Path, candidate_dir: Path, approved_for_plat: dict, approved_count, expected_sha, platform: str, required_ids: list | None) -> dict:
    """Read the serial.json report the offline runner writes. Sidecars are not required."""
    def _res(app, obs, st, rsn, unexp=None, miss=None):
        return {"approved_count": app, "observed_count": obs, "status": st, "reason": rsn, "unexpected": unexp or [], "missing": miss or []}

    try:
        doc = json.loads(serial_file.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
    except (OSError, ValueError, UnicodeDecodeError):
        return _res(approved_count, None, "mismatch", "PARSE_ERROR")
    if isinstance(doc, dict) and doc.get("lane") not in (None, "serial"):
        return _res(approved_count, None, "unknown", "LANE_MISMATCH")
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != 1
        or not isinstance(doc.get("shard"), str) or not doc.get("shard").strip()
        or "executed" not in doc
        or "sha" not in doc
    ):
        return _res(approved_count, None, "mismatch", "PARSE_ERROR")
    sha = doc.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return _res(approved_count, None, "mismatch", "PARSE_ERROR")
    if expected_sha and sha != expected_sha:
        return _res(approved_count, None, "mismatch", "STALE_RUN")
    identity = doc.get("identity")
    os_name = identity.get("os") if isinstance(identity, dict) else None
    if not isinstance(os_name, str) or not os_name.strip():
        return _res(approved_count, None, "mismatch", "PARSE_ERROR")
    reported = "windows" if "windows" in os_name.lower() else "posix"
    if reported != platform:
        return _res(approved_count, None, "mismatch", "PLATFORM_MISMATCH")
    executed = doc.get("executed")
    if not isinstance(executed, list) or not executed:
        return _res(approved_count, None, "mismatch", "PARSE_ERROR")
    if doc.get("ok") is not True:
        return _res(approved_count, None, "mismatch", "SUITE_FAILED")

    observed: Counter = Counter()
    seen_ids: list[str] = []
    for row in executed:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            return _res(approved_count, None, "mismatch", "PARSE_ERROR")
        seen_ids.append(row["id"])
        code = row.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int) or code != 0 or row.get("error") is not None:
            return _res(approved_count, None, "mismatch", "SUITE_FAILED")
        if row.get("launched") is not True:
            return _res(approved_count, None, "unknown", "NOT_LAUNCHED")
        reasons = row.get("skip_reasons")
        if not isinstance(reasons, list):
            return _res(approved_count, None, "mismatch", "PARSE_ERROR")
        for line in reasons:
            if not isinstance(line, str):
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            item = _canonical_skip(line)
            if item is None:
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            if item:
                observed[(row["id"], item)] += 1
    scan = doc.get("scan")
    if not isinstance(scan, dict) or scan.get("completed") is not True or scan.get("ok") is not True:
        return _res(approved_count, None, "unknown", "SCAN_FAILED")
    run = doc.get("run")
    if not isinstance(run, dict) or any(
        not isinstance(run.get(key), str) or not run.get(key).strip()
        for key in ("id", "attempt", "workflow")
    ):
        return _res(approved_count, None, "unknown", "RUN_UNBOUND")
    if required_ids is not None and (len(seen_ids) != len(set(seen_ids)) or set(seen_ids) != set(required_ids)):
        return _res(approved_count, sum(observed.values()), "unknown", "MISSING_SUITES")

    expected: Counter = Counter()
    for suite_id, lines in approved_for_plat.items():
        if not isinstance(suite_id, str) or not isinstance(lines, list):
            return _res(approved_count, None, "mismatch", "PARSE_ERROR")
        for line in lines:
            if not isinstance(line, str):
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            item = _canonical_skip(line)
            if not item:
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            expected[(suite_id, item)] += 1

    sidecars = sorted(candidate_dir.glob("*.skips.jsonl"))
    if sidecars:
        side: Counter = Counter()
        for path in sidecars:
            suite_name = path.name.removesuffix(".skips.jsonl")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            for line in lines:
                item = _canonical_skip(line)
                if item is None:
                    return _res(approved_count, None, "mismatch", "PARSE_ERROR")
                if item:
                    side[(suite_name, item)] += 1
        if side != observed:
            return _res(approved_count, sum(observed.values()), "unknown", "AMBIGUOUS_EVIDENCE")

    if observed != expected:
        extra = observed - expected
        missing = expected - observed
        if extra:
            return _res(
                sum(expected.values()), sum(observed.values()), "mismatch", "UNEXPECTED_SKIPS",
                unexp=[f"{suite} {item}" for (suite, item), _n in sorted(extra.items())][:20],
            )
        return _res(
            sum(expected.values()), sum(observed.values()), "mismatch", "MISSING_SKIPS",
            miss=[f"{suite} {item}" for (suite, item), _n in sorted(missing.items())][:20],
        )
    return _res(sum(expected.values()), sum(observed.values()), "match", None)


def _canonical_skip(line: str) -> str | None:
    """One skip identity, an empty string for a blank line, or None when the line is not a record."""
    s = line.strip()
    if not s:
        return ""
    if s.startswith("PBZ_SKIP "):
        s = s[len("PBZ_SKIP "):].strip()
    try:
        doc = json.loads(s, object_pairs_hook=_json_object)
    except (ValueError, TypeError):
        return None
    if isinstance(doc, dict) and isinstance(doc.get("test"), str) and isinstance(doc.get("reason"), str):
        return json.dumps({"test": doc["test"], "reason": doc["reason"]}, sort_keys=True)
    return None


def get_skips_status(
    skill_dir: Path | None = None,
    observed_sidecars_dir: Path | None = None,
    expected_sha: str | None = None,
    require_provenance: bool = False,
    platform: str | None = None,
) -> dict:
    """Compare observed test skips with approved offline_skips.json baseline across required suites."""
    def _res(app, obs, st, rsn, unexp=None, miss=None):
        return {"approved_count": app, "observed_count": obs, "status": st, "reason": rsn, "unexpected": unexp or [], "missing": miss or []}

    base_skill = skill_dir or Path(__file__).resolve().parents[1]
    manifest_path = base_skill / "tests" / "offline_skips.json"
    if not manifest_path.is_file():
        return _res(None, None, "unknown", "MISSING_MANIFEST")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
    except (OSError, ValueError, UnicodeDecodeError):
        return _res(None, None, "unknown", "PARSE_ERROR")

    if not isinstance(data, dict):
        return _res(None, None, "unknown", "PARSE_ERROR")

    plat = platform if platform in ("windows", "posix") else ("windows" if sys.platform == "win32" else "posix")
    approved_for_plat = data.get(plat)
    if not isinstance(approved_for_plat, dict):
        return _res(None, None, "unknown", "PARSE_ERROR")

    approved_suites: dict[str, set[str]] = {}
    total_approved_set: set[str] = set()
    for suite_id, suite_lines in approved_for_plat.items():
        if not isinstance(suite_lines, list):
            return _res(None, None, "unknown", "PARSE_ERROR")
        suite_set, ok = _parse_skip_records(suite_lines)
        if not ok or suite_set is None:
            return _res(None, None, "unknown", "PARSE_ERROR")
        approved_suites[suite_id] = suite_set
        total_approved_set.update(suite_set)

    approved_count = len(total_approved_set)

    candidate_dir = observed_sidecars_dir
    raw_skip = os.environ.get("PBZ_SKIP_OUTPUT")
    if not candidate_dir and raw_skip:
        p = Path(raw_skip)
        if p.is_dir():
            candidate_dir = p

    if not candidate_dir or not candidate_dir.is_dir():
        return _res(approved_count, None, "unknown", "NOT_RUN")

    serial_file = candidate_dir / "serial.json"
    if serial_file.is_file():
        required_ids = None
        suites_path = base_skill / "tests" / "offline_suites.json"
        if suites_path.is_file():
            try:
                listed = json.loads(suites_path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
            except (OSError, ValueError, UnicodeDecodeError):
                return _res(approved_count, None, "unknown", "PARSE_ERROR")
            if not isinstance(listed, list) or not listed:
                return _res(approved_count, None, "unknown", "PARSE_ERROR")
            required_ids = []
            for entry in listed:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
                    return _res(approved_count, None, "unknown", "PARSE_ERROR")
                required_ids.append(entry["id"])
            if len(required_ids) != len(set(required_ids)):
                return _res(approved_count, None, "unknown", "PARSE_ERROR")
        return _serial_skip_status(
            serial_file, candidate_dir, approved_for_plat, approved_count, expected_sha, plat, required_ids,
        )

    # 1. Discover provenance and validate presence, SHA, and platform first
    # (stale SHA / wrong OS fails closed to STALE_RUN / PLATFORM_MISMATCH before parsing sidecars)
    prov_file = next((candidate_dir / name for name in ("provenance.json", "report.json") if (candidate_dir / name).is_file()), None)
    if (expected_sha or require_provenance) and not prov_file:
        return _res(approved_count, None, "unknown", "NOT_RUN")

    prov_data = None
    if prov_file:
        try:
            prov_data = json.loads(prov_file.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
            if not isinstance(prov_data, dict):
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            p_sha = prov_data.get("sha")
            p_plat = prov_data.get("platform")
            if not isinstance(p_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", p_sha) or not isinstance(p_plat, str):
                return _res(approved_count, None, "mismatch", "PARSE_ERROR")
            if expected_sha and p_sha != expected_sha:
                return _res(approved_count, None, "mismatch", "STALE_RUN")
            if p_plat.strip().lower() != plat.strip().lower():
                return _res(approved_count, None, "mismatch", "PLATFORM_MISMATCH")
        except (OSError, ValueError, UnicodeDecodeError):
            return _res(approved_count, None, "mismatch", "PARSE_ERROR")

    # 2. Check known provenance execution failure before parsing sidecars
    if prov_data:
        p_exit = prov_data.get("exit_code")
        p_st = prov_data.get("status")
        if p_exit is not None and (isinstance(p_exit, bool) or not isinstance(p_exit, int) or p_exit != 0):
            return _res(approved_count, None, "mismatch", "SUITE_FAILED")
        if p_st is not None:
            if not isinstance(p_st, str):
                return _res(approved_count, None, "mismatch", "SUITE_FAILED")
            st_clean = p_st.strip().lower()
            if st_clean == "in_progress":
                return _res(approved_count, None, "unknown", "NOT_RUN")
            if st_clean not in ("passed", "success", "ok"):
                return _res(approved_count, None, "mismatch", "SUITE_FAILED")

    # 3. Discover and parse sidecars
    observed_files = list(candidate_dir.glob("*.skips.jsonl"))
    if not observed_files:
        return _res(approved_count, None, "unknown", "NOT_RUN")

    observed_by_suite: dict[str, set[str]] = {}
    total_observed_set: set[str] = set()
    for f in observed_files:
        suite_name = f.name.removesuffix(".skips.jsonl")
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return _res(approved_count, None, "mismatch", "PARSE_ERROR")
        s_set, ok = _parse_skip_records(lines)
        if not ok or s_set is None:
            return _res(approved_count, None, "mismatch", "PARSE_ERROR")
        observed_by_suite[suite_name] = s_set
        total_observed_set.update(s_set)

    obs_count = len(total_observed_set)

    # 4. Unexpected extra suite sidecars trigger UNEXPECTED_SKIPS (exit 1)
    unexpected_suites = [s for s in observed_by_suite if s not in approved_suites]
    if unexpected_suites:
        return _res(approved_count, obs_count, "mismatch", "UNEXPECTED_SKIPS", unexp=[f"suite:{s}" for s in sorted(unexpected_suites)])

    # 5. Unexpected skip records in observed approved suites trigger UNEXPECTED_SKIPS (exit 1)
    all_unexpected = []
    for s, obs_set in observed_by_suite.items():
        app_set = approved_suites.get(s, set())
        all_unexpected.extend(obs_set - app_set)
    if all_unexpected:
        return _res(approved_count, obs_count, "mismatch", "UNEXPECTED_SKIPS", unexp=sorted(all_unexpected)[:20])

    # 6. Affirmative provenance execution completion required when provenance present
    if prov_data:
        p_exit = prov_data.get("exit_code")
        p_st = prov_data.get("status")
        if p_exit is None or p_st is None:
            return _res(approved_count, obs_count, "unknown", "NOT_RUN")

    # 7. Missing suites
    missing_suites = [s for s in approved_suites if s not in observed_by_suite]
    if missing_suites:
        return _res(approved_count, obs_count, "unknown", "MISSING_SKIPS", miss=[f"suite:{s}" for s in sorted(missing_suites)])

    # 8. Missing skip records in observed approved suites
    all_missing = []
    for s, app_set in approved_suites.items():
        obs_set = observed_by_suite.get(s, set())
        all_missing.extend(app_set - obs_set)
    if all_missing:
        return _res(approved_count, obs_count, "mismatch", "MISSING_SKIPS", miss=sorted(all_missing)[:20])

    return _res(approved_count, obs_count, "match", None)


def _desktop_process_available() -> bool | None:
    """Whether a Desktop process is running. That fact is not a live test result."""
    try:
        return bool(desktop.processes())
    except Exception:
        return None


def _live_acceptance(text: str, expected_sha: str | None) -> str:
    """verified, conflict, or absent. Line order is not a clock."""
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        return "absent"
    grouped: dict[str, set[str]] = {}
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(doc, dict):
            continue
        revision = doc.get("revision") if isinstance(doc.get("revision"), str) else doc.get("sha")
        case = doc.get("case")
        result = doc.get("result")
        if revision != expected_sha or not isinstance(case, str) or not case.strip():
            continue
        if result not in ("PASS", "FAIL"):
            continue
        grouped.setdefault(case, set()).add(result)
    if any("PASS" in results and "FAIL" in results for results in grouped.values()):
        return "conflict"
    if any(results == {"PASS"} for results in grouped.values()):
        return "verified"
    return "absent"


def _verified_live_case(text: str, expected_sha: str | None) -> bool:
    """True only when a named case passed and no named case conflicts."""
    return _live_acceptance(text, expected_sha) == "verified"


def get_desktop_evidence(ws_path: str | Path | None = None, expected_sha: str | None = None) -> tuple[bool | None, str | None]:
    """Live synthetic acceptance for a named case. A running process is not acceptance."""
    if ws_path:
        log_file = Path(ws_path).resolve() / ".pbz" / "private" / "evidence.log"
        if log_file.is_file():
            try:
                acceptance = _live_acceptance(
                    log_file.read_text(encoding="utf-8", errors="replace"), expected_sha
                )
                if acceptance == "verified":
                    return True, None
                if acceptance == "conflict":
                    return False, "CONFLICTING_EVIDENCE"
            except (OSError, UnicodeDecodeError):
                pass
    return (False, "NOT_RUN") if sys.platform == "win32" else (None, "UNAVAILABLE")


def posix_exemption_status(doc, expected_ids: list | None) -> dict:
    """Read a POSIX exemption report. This is not serial coverage for either OS."""
    def _res(st, rsn, lane="posix"):
        return {"status": st, "reason": rsn, "lane": lane}

    if not isinstance(doc, dict) or doc.get("schema") != 1 or doc.get("lane") != "posix":
        return _res("unknown", "PARSE_ERROR", None)
    if not isinstance(doc.get("os"), str) or not doc.get("os").strip():
        return _res("unknown", "PARSE_ERROR")
    rows = doc.get("tests")
    if not isinstance(rows, list) or not isinstance(expected_ids, list):
        return _res("unknown", "PARSE_ERROR")
    seen: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            return _res("unknown", "PARSE_ERROR")
        if row.get("outcome") != "passed":
            return _res("mismatch", "SUITE_FAILED")
        seen.append(row["id"])
    if len(seen) != len(set(seen)) or set(seen) != set(expected_ids):
        return _res("unknown", "MISSING_SUITES")
    if doc.get("passed") is not True:
        return _res("mismatch", "SUITE_FAILED")
    return _res("verified", None)


def _posix_lane_report(candidate_dir: Path | None, skill_dir: Path | None = None) -> dict:
    """Diagnostic only. A verified exemption lane does not make the serial gate succeed."""
    if candidate_dir is None or not (candidate_dir / "posix.json").is_file():
        return {"status": "unknown", "reason": "NOT_RUN", "lane": "posix"}
    base = skill_dir or Path(__file__).resolve().parents[1]
    try:
        expected_doc = json.loads((base / "tests" / "posix_tests.json").read_text(encoding="utf-8"))
        report = json.loads((candidate_dir / "posix.json").read_text(encoding="utf-8"), object_pairs_hook=_json_object)
    except (OSError, ValueError, UnicodeDecodeError):
        return {"status": "unknown", "reason": "PARSE_ERROR", "lane": "posix"}
    expected = expected_doc.get("tests") if isinstance(expected_doc, dict) else None
    return posix_exemption_status(report, expected if isinstance(expected, list) else None)


def evaluate_ci_outcome(ci_data: dict) -> str:
    def _dict(v) -> dict:
        return v if isinstance(v, dict) else {}

    hc, bp, sk = _dict(ci_data.get("hosted_checks")), _dict(ci_data.get("branch_protection")), _dict(ci_data.get("skips"))

    # 1. Definitive failures (highest precedence)
    if hc.get("status") == "failure" or bp.get("status") == "unprotected":
        return "FAILED"
    if sk.get("status") == "mismatch":
        reason = sk.get("reason")
        if reason in ("STALE_RUN", "PLATFORM_MISMATCH", "PARSE_ERROR", "MISSING_SKIPS", "AMBIGUOUS_EVIDENCE", "MISSING_SUITES"):
            return "INCOMPLETE"
        return "FAILED"

    # 2. Strict affirmative allow-list for SUCCEEDED:
    sha = ci_data.get("sha")
    is_valid_sha = isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{40}", sha))

    raw_checks = hc.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        return "INCOMPLETE"
    for c in raw_checks:
        if not isinstance(c, dict):
            return "INCOMPLETE"

    # Require at least one completed successful check in the evidence
    has_completed_success = any(
        c.get("status") == "completed" and c.get("conclusion") == "success"
        for c in raw_checks
    )
    if not has_completed_success:
        return "INCOMPLETE"

    # Reconcile required checks (including app constraints) with successful hosted check runs
    raw_req_checks = bp.get("required_checks")
    if raw_req_checks is not None:
        if not isinstance(raw_req_checks, list):
            return "INCOMPLETE"
        req_checks = []
        for r in raw_req_checks:
            if not isinstance(r, dict) or not isinstance(r.get("context"), str):
                return "INCOMPLETE"
            req_checks.append(r)
    else:
        raw_req_ctxs = bp.get("required_contexts")
        if raw_req_ctxs is not None and not isinstance(raw_req_ctxs, list):
            return "INCOMPLETE"
        req_checks = [{"context": c, "app_id": None} for c in (raw_req_ctxs or []) if isinstance(c, str)]

    required_checks_satisfied = True
    for req in req_checks:
        ctx_name = req["context"]
        expected_app = req.get("app_id")
        if req.get("kind") in ("workflow", "unproven") or not _context_proven(raw_checks, ctx_name, expected_app):
            required_checks_satisfied = False
            break
        if expected_app is not None and not _context_proven(raw_checks, ctx_name, None):
            required_checks_satisfied = False
            break

    raw_bp_ctxs = bp.get("required_contexts")
    if raw_bp_ctxs is not None:
        if not isinstance(raw_bp_ctxs, list) or not all(isinstance(c, str) for c in raw_bp_ctxs):
            return "INCOMPLETE"
        req_ctxs = set(raw_bp_ctxs)
    else:
        req_ctxs = set()

    if req_ctxs and not all(_context_proven(raw_checks, ctx, None) for ctx in req_ctxs):
        required_checks_satisfied = False

    if (
        is_valid_sha
        and hc.get("checked_sha") == sha
        and ci_data.get("dirty") is False
        and ci_data.get("hook_mode") == "full"
        and hc.get("status") == "success"
        and bp.get("status") == "protected"
        and required_checks_satisfied
        and sk.get("status") == "match"
        and ci_data.get("artifact_provenance") == "authenticated"
    ):
        return "SUCCEEDED"

    # 3. Everything else fails closed to INCOMPLETE (exit 3)
    return "INCOMPLETE"


def _graft_lines(repo_root: Path) -> bool | None:
    """True when info/grafts names a parent. None when that file cannot be read."""
    rc, git_path = _run_git(["rev-parse", "--git-path", "info/grafts"], cwd=repo_root)
    if rc != 0 or not git_path:
        return False
    graft_file = Path(git_path)
    if not graft_file.is_absolute():
        graft_file = repo_root / graft_file
    try:
        if not graft_file.is_file():
            return False
        text = graft_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            return True
    return False


def cmd_doctor_ci(
    ws_path: str | Path | None = None,
    offline: bool = False,
    timeout: int = 5,
    observed_sidecars_dir: Path | None = None,
    branch_override: str | None = None,
) -> dict:
    start_dir = Path(ws_path) if ws_path else Path.cwd()
    try:
        resolved_start = start_dir.resolve()
    except OSError:
        resolved_start = None
    if resolved_start is None or _root_collapsed(start_dir, resolved_start):
        repo_root = None
    else:
        repo_root = _find_repo_root(resolved_start)

    sha, sha_reason = get_head_sha(repo_root)
    dirty, dirty_reason = get_dirty_status(repo_root)
    again, _again_reason = get_head_sha(repo_root)
    if again != sha:
        dirty, dirty_reason = None, "DIFF_INDEX_FAILED"
    hook_mode = get_hook_mode(repo_root)
    current_branch, branch_reason = get_current_branch(repo_root)

    is_override = isinstance(branch_override, str) and bool(branch_override)
    target_branch = branch_override if is_override else current_branch
    origin_slug = _UNBOUND_SLUG
    origin_url = None
    if repo_root and not offline:
        origin_slug, origin_url = get_origin(repo_root)

    def _bp_err(reason: str) -> dict:
        return {"branch": target_branch, "status": "unknown", "reason": reason, "ruleset": None, "required_contexts": [], "required_checks": []}

    if target_branch and (
        target_branch != target_branch.strip()
        or target_branch.startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_./@+-]+", target_branch)
    ):
        branch_protection = _bp_err("BRANCH_MISMATCH")
    elif is_override and not repo_root:
        branch_protection = _bp_err("BRANCH_MISMATCH")
    elif repo_root:
        rc_ref, resolved_ref = _run_git(["rev-parse", "--verify", "--end-of-options", f"refs/remotes/origin/{target_branch}"], cwd=repo_root)
        if rc_ref != 0:
            branch_protection = _bp_err("BRANCH_MISMATCH")
        else:
            tip = resolved_ref.strip()
            if not offline:
                remote_sha = ""
                if origin_url and origin_slug:
                    rc_map, mapped = _run_git(
                        ["ls-remote", "--get-url", "--end-of-options", origin_url],
                        cwd=repo_root,
                    )
                    if rc_map == 0 and mapped == origin_url:
                        rc_ls, ls_out = _run_git(
                            ["ls-remote", "--end-of-options", origin_url, f"refs/heads/{target_branch}"],
                            cwd=repo_root,
                        )
                    else:
                        rc_ls, ls_out = 1, ""
                    want_ref = f"refs/heads/{target_branch}"
                    if rc_ls == 0 and ls_out:
                        for line in ls_out.splitlines():
                            parts = line.split()
                            if len(parts) >= 2 and parts[1] == want_ref and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                                remote_sha = parts[0]
                                break
                tip = remote_sha
            if not tip or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                branch_protection = _bp_err("BRANCH_MISMATCH")
            else:
                grafts = _graft_lines(repo_root)
                if grafts is None or grafts:
                    branch_protection = _bp_err("BRANCH_MISMATCH")
                else:
                    rc_anc, _ = _run_git(["merge-base", "--is-ancestor", "--end-of-options", sha, tip], cwd=repo_root)
                    if rc_anc != 0:
                        branch_protection = _bp_err("BRANCH_MISMATCH")
                    else:
                        branch_protection = get_branch_protection(repo_root, target_branch, branch_reason=branch_reason, offline=offline, timeout=timeout, origin_slug=origin_slug)
    else:
        branch_protection = get_branch_protection(repo_root, target_branch, branch_reason=branch_reason, offline=offline, timeout=timeout, origin_slug=origin_slug)

    req_contexts = branch_protection.get("required_contexts", [])
    req_checks = branch_protection.get("required_checks", [])
    hosted_checks = get_hosted_checks(
        repo_root, sha, offline=offline, timeout=timeout,
        required_contexts=req_contexts, required_checks=req_checks,
        origin_slug=origin_slug,
    )
    if branch_protection.get("status") not in ("protected", "unprotected") and hosted_checks.get("status") == "success":
        hosted_checks = dict(hosted_checks)
        hosted_checks["status"] = "unknown"
        hosted_checks["reason"] = "REQUIREMENTS_UNKNOWN"
    skips = get_skips_status(skill_dir=None, observed_sidecars_dir=observed_sidecars_dir, expected_sha=sha, require_provenance=True)
    desktop_present, desktop_reason = get_desktop_evidence(ws_path, expected_sha=sha)
    if hook_mode == "full":
        hook_mode_reason = None
    elif hook_mode == "main-offline-required":
        hook_mode_reason = "LOCAL_OPT_IN_PRE_COMMIT_SKIPS_PRE_PUSH_FULL"
    elif hook_mode == "fast":
        hook_mode_reason = "HOOK_MODE_FAST"
    elif hook_mode == "not_installed":
        hook_mode_reason = "NOT_INSTALLED"
    elif hook_mode == "foreign":
        hook_mode_reason = "FOREIGN_HOOK"
    else:
        hook_mode_reason = "GIT_UNAVAILABLE" if not repo_root else "UNAVAILABLE"

    ci_data = {
        "sha": sha, "sha_reason": sha_reason,
        "dirty": dirty, "dirty_reason": dirty_reason,
        "hook_mode": hook_mode, "hook_mode_reason": hook_mode_reason,
        "hosted_checks": hosted_checks,
        "branch_protection": branch_protection,
        "skips": skips,
        "desktop_evidence": desktop_present,
        "desktop_evidence_reason": desktop_reason,
        "desktop_available": _desktop_process_available(),
        # Local files are not hosted artifacts. Retrieval is a separate design.
        "artifact_provenance": "unavailable",
        "artifact_provenance_reason": "RETRIEVAL_UNAVAILABLE",
        "posix_lane": _posix_lane_report(observed_sidecars_dir),
    }

    return {
        "verb": "doctor",
        "target": "ci",
        "status": evaluate_ci_outcome(ci_data),
        "ci": ci_data,
    }
