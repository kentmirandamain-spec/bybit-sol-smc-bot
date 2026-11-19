#!/usr/bin/env python3
"""In-place updates from GitHub for the SOL SMC bot.

The dashboard calls this module. It uses only the Python standard library
(urllib, zipfile, http.client) so it can run on a PC where Smart App Control
blocks new compiled packages, and it does not need git.

User data is never replaced: .env, logs/, state/ (except the install stamp
state/update.json), .venv/, backups/, and the user's existing config.yaml
values. Replaced files are copied to backups/<timestamp>/ first.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin

REPO = "kentmirandamain-spec/bybit-sol-smc-bot"
API_ROOT = f"https://api.github.com/repos/{REPO}"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPO}/main"
USER_AGENT = "bybit-sol-smc-bot-updater"
ALLOWED_HOSTS = {"api.github.com", "raw.githubusercontent.com", "codeload.github.com", "github.com"}
TOKEN_HOSTS = {"api.github.com", "raw.githubusercontent.com"}

MAX_ZIP_FILES = 500
MAX_FILE_BYTES = 20_000_000
MAX_TOTAL_BYTES = 80_000_000
PRESERVE_ROOTS = {".venv", "logs", "state", "backups", "__pycache__", ".git", "__MACOSX"}
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$")


class UpdateError(Exception):
    """A problem the dashboard should show to the user as-is."""


@dataclass
class FetchResponse:
    status: int
    body: bytes
    headers: dict
    url: str


@dataclass
class ApplyResult:
    ok: bool
    summary: str
    output: str
    version: str = ""
    commit: str | None = None
    backup_id: str | None = None
    requirements_changed: bool = False
    pip_ok: bool | None = None
    files_updated: list[str] = field(default_factory=list)
    files_added: list[str] = field(default_factory=list)
    config_keys_added: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def redact(text: str, token: str | None = None) -> str:
    """Remove tokens from text that might be shown in the dashboard."""
    if not text:
        return ""
    out = text
    if token:
        out = out.replace(token, "***")
    out = re.sub(r"(?i)(authorization:\s*(?:bearer|token)\s+)\S+", r"\1***", out)
    out = re.sub(r"(?i)([?&](?:access_)?token=)[^&\s\"']+", r"\1***", out)
    out = re.sub(r"(?i)(GITHUB_TOKEN\s*=\s*)\S+", r"\1***", out)
    return out


def github_token(root: Path | None = None) -> str | None:
    """Read GITHUB_TOKEN from the environment or, if missing, from root/.env."""
    env = os.getenv("GITHUB_TOKEN", "").strip().strip('"').strip("'")
    if env:
        return env
    if root is None:
        return None
    return _env_file_value(root / ".env", "GITHUB_TOKEN")


def _env_file_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        if k.strip() == key:
            value = v.strip().strip('"').strip("'")
            return value or None
    return None


def read_version(root: Path) -> str:
    try:
        text = (root / "VERSION").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line if _VERSION_RE.fullmatch(line) else "unknown"


def read_installed(root: Path) -> dict:
    path = root / "state" / "update.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _same_commit(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return len(short) >= 7 and long.startswith(short)


def is_update_available(local_version: str, remote_version: str,
                        local_commit: str | None, remote_commit: str | None) -> bool:
    """True when main is not the version installed in this folder."""
    if _same_commit(local_commit, remote_commit):
        return False
    if local_version == "unknown" and remote_version not in ("", "unknown"):
        return True
    if (local_version not in ("", "unknown") and remote_version not in ("", "unknown")
            and local_version != remote_version):
        return True
    if local_commit and remote_commit and not _same_commit(local_commit, remote_commit):
        return True
    return False


def trading_block_reason(bot: dict | None) -> str | None:
    """Refuse an update/rollback while the bot is running with a position or order.

    A stopped bot may still have the last position echoed in status.json; that
    does not block an update. Exchange-side stops stay on Bybit either way.
    """
    if not bot or not bot.get("alive"):
        return None
    pos = bot.get("position")
    pend = bot.get("pending_order")
    if not pos and not pend:
        return None
    bits = []
    if pos:
        side = pos.get("side") if isinstance(pos, dict) else None
        bits.append("an open position" + (f" ({side})" if side else ""))
    if pend:
        bits.append("a pending order")
    return (
        "Stop the bot first. It is running with "
        + " and ".join(bits)
        + ". Stopping does not cancel a stop-loss or take-profit already on the exchange."
    )


def changelog_excerpt(text: str, local_version: str, limit: int = 5000) -> str:
    """Return the changelog above the heading for the version already installed."""
    if not text or not text.strip():
        return ""
    lines = text.splitlines()
    cut = None
    if local_version and local_version != "unknown":
        pat = re.compile(rf"^#+\s+.*\b{re.escape(local_version)}\b")
        for i, line in enumerate(lines):
            if pat.search(line):
                cut = i
                break
    body = "\n".join(lines[:cut] if cut else lines).strip()
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n…"
    return body


# --------------------------------------------------------------------------- #
# GitHub HTTP (manual redirects, host allow-list, token stays off codeload)
# --------------------------------------------------------------------------- #
def _validate_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UpdateError("Refusing a non-HTTPS update URL.")
    if parts.username or parts.password:
        raise UpdateError("Refusing an update URL that contains credentials.")
    host = parts.hostname.lower()
    if host not in ALLOWED_HOSTS:
        raise UpdateError(f"Refusing to contact {host}.")
    if parts.port not in (None, 443):
        raise UpdateError("Refusing an update URL on a non-standard port.")


def _accept_for(url: str) -> str:
    if "/zipball/" in url or url.endswith("/VERSION") or url.endswith(".md"):
        return "*/*"
    return "application/vnd.github+json"


def _request_headers(url: str, token: str | None, accept: str) -> dict:
    """Authorization is sent only to the GitHub API and raw hosts, never to codeload."""
    parts = urlsplit(url)
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    host = parts.hostname.lower() if parts.hostname else ""
    if token and host in TOKEN_HOSTS:
        headers["Authorization"] = "Bearer " + token
    return headers


def _https_request(url: str, token: str | None, timeout: float, accept: str, max_bytes: int):
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    headers = _request_headers(url, token, accept)
    conn = http.client.HTTPSConnection(parts.hostname, 443, timeout=timeout,
                                       context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        body = resp.read(max_bytes + 1)
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(redact(f"Could not reach GitHub: {e}", token)) from None
    finally:
        conn.close()
    if len(body) > max_bytes:
        raise UpdateError("GitHub response is too large.")
    return status, hdrs, body


def http_get(url: str, *, token: str | None = None, timeout: float = 30,
             max_bytes: int = 2_000_000, request=None) -> FetchResponse:
    """GET a GitHub URL, following redirects only to known GitHub hosts."""
    current = url
    do = request or _https_request
    for _ in range(6):
        _validate_url(current)
        status, headers, body = do(current, token, timeout, _accept_for(current), max_bytes)
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("location")
            if not loc:
                raise UpdateError("GitHub redirect was missing a location.")
            current = urljoin(current, loc.strip())
            continue
        if status in (401, 403, 404):
            detail = redact(body.decode("utf-8", "replace")[:180], token).strip()
            # "token" in a GitHub error body is not our secret; still drop it if it
            # looks like the credential we sent.
            if token and token in (detail or ""):
                detail = detail.replace(token, "***")
            if status == 404 and not token:
                raise UpdateError(
                    "GitHub returned HTTP 404. If this repository is public, main may not "
                    "have that file yet. If it is private, set GITHUB_TOKEN in .env "
                    "(a read-only fine-grained token with Contents: Read is enough)."
                )
            hint = ""
            if not token and status in (401, 403):
                hint = (" If the repository is private, set GITHUB_TOKEN in .env "
                        "(a read-only fine-grained token with Contents: Read is enough).")
            extra = f" {detail}" if detail else ""
            raise UpdateError(f"GitHub returned HTTP {status}.{hint}{extra}")
        if status >= 400:
            detail = redact(body.decode("utf-8", "replace")[:300], token)
            raise UpdateError(f"GitHub returned HTTP {status}: {detail}")
        return FetchResponse(status, body, headers, current)
    raise UpdateError("Too many redirects talking to GitHub.")


def _json_body(resp: FetchResponse):
    try:
        return json.loads(resp.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise UpdateError("GitHub returned something that was not JSON.")


def _commit_entry(obj: dict) -> dict | None:
    if not isinstance(obj, dict):
        return None
    sha = str(obj.get("sha") or "")
    commit = obj.get("commit") if isinstance(obj.get("commit"), dict) else {}
    message = str(commit.get("message") or "")
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    date = str(author.get("date") or "")
    first = message.splitlines()[0].strip()[:180] if message else ""
    if not sha and not first:
        return None
    return {"sha": sha[:12], "message": first, "date": date}


def _parse_commit_obj(data: dict) -> tuple[str, str, str]:
    entry = _commit_entry(data) or {}
    sha = str(data.get("sha") or "")
    return sha, entry.get("message") or "", entry.get("date") or ""


def _parse_commit_list(data) -> list[dict]:
    rows = data if isinstance(data, list) else []
    out = []
    for obj in rows[:20]:
        entry = _commit_entry(obj)
        if entry:
            out.append(entry)
    return out


def _parse_compare(data) -> list[dict]:
    if not isinstance(data, dict):
        return []
    out = []
    for obj in (data.get("commits") or [])[-20:]:
        entry = _commit_entry(obj)
        if entry:
            out.append(entry)
    return out


def _write_installed(root: Path, version: str, commit: str | None) -> None:
    payload = {
        "version": version,
        "commit": commit,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = root / "state" / "update.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def check_for_updates(root: Path, *, token: str | None = None, fetch=None) -> dict:
    """Compare this folder with main. On a matching version, remember the commit."""
    local_version = read_version(root)
    installed = read_installed(root)
    local_commit = installed.get("commit") if isinstance(installed.get("commit"), str) else None
    if local_commit and not _SHA_RE.fullmatch(local_commit):
        local_commit = None
    base = {
        "local_version": local_version,
        "remote_version": None,
        "local_commit": local_commit,
        "remote_commit": None,
        "remote_message": "",
        "update_available": False,
        "commits": [],
        "changelog": "",
        "error": None,
    }
    if fetch is None:
        def fetch(url, _token=token):
            return http_get(url, token=_token, timeout=25, max_bytes=2_000_000)

    try:
        head = _json_body(fetch(f"{API_ROOT}/commits/main"))
        if not isinstance(head, dict):
            raise UpdateError("GitHub did not return the main branch commit.")
        remote_commit, remote_message, _date = _parse_commit_obj(head)
        ver_resp = fetch(f"{RAW_ROOT}/VERSION")
        remote_version = ver_resp.body.decode("utf-8", "replace").strip().splitlines()[0].strip()
        if not _VERSION_RE.fullmatch(remote_version):
            remote_version = "unknown"
        changelog = ""
        try:
            log_resp = fetch(f"{RAW_ROOT}/CHANGELOG.md")
            changelog = changelog_excerpt(log_resp.body.decode("utf-8", "replace"), local_version)
        except UpdateError:
            changelog = ""
        commits: list[dict] = []
        if local_commit:
            try:
                cmp_resp = fetch(f"{API_ROOT}/compare/{local_commit}...main")
                commits = _parse_compare(_json_body(cmp_resp))
            except UpdateError:
                try:
                    commits = _parse_commit_list(_json_body(fetch(f"{API_ROOT}/commits?sha=main&per_page=20")))
                except UpdateError:
                    commits = []
        else:
            try:
                commits = _parse_commit_list(_json_body(fetch(f"{API_ROOT}/commits?sha=main&per_page=20")))
            except UpdateError:
                commits = []
        # First successful check on the current version pins the commit so later
        # commits show up even when VERSION was not bumped.
        if (not local_commit and remote_commit and _SHA_RE.fullmatch(remote_commit)
                and local_version == remote_version and local_version != "unknown"):
            try:
                _write_installed(root, local_version, remote_commit)
                local_commit = remote_commit
            except OSError:
                pass
        available = is_update_available(local_version, remote_version, local_commit, remote_commit)
        base.update(remote_version=remote_version, local_commit=local_commit,
                    remote_commit=remote_commit, remote_message=remote_message,
                    update_available=available, commits=commits if available or local_commit else commits[:8],
                    changelog=changelog if available else (changelog if local_version != remote_version else ""))
        if not available:
            base["changelog"] = ""
        return base
    except UpdateError as e:
        base["error"] = str(e)
        return base


# --------------------------------------------------------------------------- #
# config.yaml merge: keep the user's values and comments, add new keys
# --------------------------------------------------------------------------- #
@dataclass
class _YNode:
    key: str
    indent: int
    line: int
    end: int
    inline: str
    children: list


def _blank_or_comment(line: str) -> bool:
    s = line.strip()
    return s == "" or s.startswith("#")


def _strip_inline_comment(text: str) -> str:
    out = []
    quote = None
    for ch in text:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _parse_level(lines: list[str], start: int, end: int, indent: int) -> list[_YNode]:
    nodes = []
    i = start
    while i < end:
        if _blank_or_comment(lines[i]):
            i += 1
            continue
        match = _KEY_RE.match(lines[i])
        if not match:
            i += 1
            continue
        ind = len(match.group(1))
        if ind < indent:
            break
        if ind > indent:
            i += 1
            continue
        j = i + 1
        while j < end:
            if _blank_or_comment(lines[j]):
                j += 1
                continue
            nxt = _KEY_RE.match(lines[j])
            if nxt and len(nxt.group(1)) <= ind:
                break
            j += 1
        k = j - 1
        while k > i and _blank_or_comment(lines[k]):
            k -= 1
        content_end = k + 1
        inline = _strip_inline_comment(match.group(3)).strip()
        children: list[_YNode] = []
        if inline == "":
            child_indent = None
            for t in range(i + 1, content_end):
                if _blank_or_comment(lines[t]):
                    continue
                child = _KEY_RE.match(lines[t])
                if child and len(child.group(1)) > ind:
                    child_indent = len(child.group(1))
                    break
            if child_indent is not None:
                children = _parse_level(lines, i + 1, content_end, child_indent)
        nodes.append(_YNode(match.group(2), ind, i, content_end, inline, children))
        i = j
    return nodes


def _comment_start(lines: list[str], key_line: int) -> int:
    i = key_line - 1
    while i >= 0 and lines[i].strip() == "":
        if i - 1 >= 0 and lines[i - 1].strip().startswith("#"):
            i -= 1
            continue
        break
    while i >= 0 and lines[i].strip().startswith("#"):
        i -= 1
    return i + 1


def _dotted_leaves(node: _YNode, prefix: str) -> list[str]:
    name = f"{prefix}.{node.key}" if prefix else node.key
    if not node.children:
        return [name]
    out = []
    for child in node.children:
        out.extend(_dotted_leaves(child, name))
    return out or [name]


def merge_config_text(user_text: str, new_text: str) -> tuple[str, list[str]]:
    """Return (merged text, dotted keys that were added from new_text).

    Existing user lines, including comments and values, are left as they are.
    Keys present only in the new default are inserted with their default text.
    """
    if user_text == new_text:
        return user_text, []
    user_lines = user_text.splitlines()
    new_lines = new_text.splitlines()
    user_nodes = _parse_level(user_lines, 0, len(user_lines), 0)
    new_nodes = _parse_level(new_lines, 0, len(new_lines), 0)
    inserts: list[tuple[int, list[str], list[str]]] = []

    def walk(unodes: list[_YNode], nnodes: list[_YNode], parent: _YNode | None, prefix: str) -> None:
        have = {n.key: n for n in unodes}
        for node in nnodes:
            mine = have.get(node.key)
            if mine is None:
                at = len(user_lines) if parent is None else parent.end
                start = _comment_start(new_lines, node.line)
                block = new_lines[start:node.end]
                inserts.append((at, block, _dotted_leaves(node, prefix)))
                continue
            if mine.inline == "" and node.inline == "" and node.children:
                dotted = f"{prefix}.{node.key}" if prefix else node.key
                walk(mine.children, node.children, mine, dotted)

    walk(user_nodes, new_nodes, None, "")
    if not inserts:
        return user_text, []

    added: list[str] = []
    for at, block, keys in inserts:
        added.extend(keys)
    lines = user_lines
    for at, block, _keys in reversed(inserts):
        if not block:
            continue
        chunk = list(block)
        first = chunk[0]
        if at > 0 and lines and lines[at - 1].strip() != "" and not first[:1].isspace():
            chunk.insert(0, "")
        if at < len(lines) and lines[at].strip() != "" and chunk[-1].strip() != "":
            chunk.append("")
        lines[at:at] = chunk
    nl = "\r\n" if "\r\n" in user_text else "\n"
    body = nl.join(lines)
    if user_text.endswith(("\n", "\r\n")) and not body.endswith(("\n", "\r\n")):
        body += nl
    elif not body.endswith("\n"):
        body += nl
    return body, added


def iter_yaml_keys(text: str) -> list[str]:
    """Dotted keys, for tests."""
    lines = text.splitlines()
    nodes = _parse_level(lines, 0, len(lines), 0)

    def walk(nodes, prefix=""):
        out = []
        for n in nodes:
            name = f"{prefix}.{n.key}" if prefix else n.key
            out.append(name)
            out.extend(walk(n.children, name))
        return out

    return walk(nodes)


# --------------------------------------------------------------------------- #
# zip apply + rollback
# --------------------------------------------------------------------------- #
def is_preserved(rel: str) -> bool:
    parts = Path(rel.replace("\\", "/")).parts
    if not parts:
        return True
    if parts[0] in PRESERVE_ROOTS:
        return True
    name = parts[-1]
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name == "backtest_trades.csv":
        return True
    if name.endswith(".bak") or name.endswith(".tmp"):
        return True
    if name == ".DS_Store":
        return True
    return False


def safe_target(root: Path, rel: str) -> Path:
    rel_path = Path(rel.replace("\\", "/"))
    if rel_path.is_absolute() or ".." in rel_path.parts or rel_path.drive:
        raise UpdateError(f"Refusing path {rel}")
    root_r = root.resolve()
    target = (root_r / rel_path).resolve()
    if target != root_r and root_r not in target.parents:
        raise UpdateError(f"Refusing path {rel}")
    return target


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _zip_files(data: bytes) -> tuple[str | None, list[tuple[str, bytes]]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("Download was not a zip archive.")
    with zf:
        if len(zf.infolist()) > MAX_ZIP_FILES:
            raise UpdateError("Update archive has too many files.")
        named = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw_name = info.filename.replace("\\", "/")
            if raw_name.startswith("/") or re.match(r"^[A-Za-z]:", raw_name) or "\x00" in raw_name:
                raise UpdateError("Update archive has an unsafe path.")
            parts = [p for p in raw_name.split("/") if p not in ("", ".")]
            if not parts or ".." in parts or any(":" in p for p in parts):
                raise UpdateError("Update archive has an unsafe path.")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateError(f"Update archive contains a symlink: {raw_name}")
            named.append((info, "/".join(parts)))
        if not named:
            raise UpdateError("The download did not contain any project files.")
        firsts = {n.split("/")[0] for _, n in named}
        strip = len(firsts) == 1 and all("/" in n for _, n in named)
        prefix = next(iter(firsts)) if strip else None
        out = []
        total = 0
        for info, name in named:
            rel = name.split("/", 1)[1] if strip else name
            if not rel:
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise UpdateError(f"File in the update is too large: {rel}")
            with zf.open(info) as src:
                chunks = []
                nread = 0
                while True:
                    blob = src.read(65536)
                    if not blob:
                        break
                    nread += len(blob)
                    if nread > MAX_FILE_BYTES:
                        raise UpdateError(f"File in the update is too large: {rel}")
                    chunks.append(blob)
            total += nread
            if total > MAX_TOTAL_BYTES:
                raise UpdateError("Update archive is too large.")
            out.append((rel, b"".join(chunks)))
        return prefix, out


def _sha_from_prefix(prefix: str | None) -> str | None:
    if not prefix:
        return None
    last = prefix.split("-")[-1]
    if _SHA_RE.fullmatch(last):
        return last.lower()
    return None


def _backup_id(now: datetime | None = None) -> str:
    when = now or datetime.now(timezone.utc)
    return when.strftime("%Y%m%dT%H%M%SZ")


def _norm_newlines(data: bytes | None) -> bytes | None:
    if data is None:
        return None
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def apply_zip_bytes(root: Path, data: bytes, *, commit: str | None = None,
                    now: datetime | None = None) -> dict:
    """Replace project files from a GitHub zipball. Returns a manifest dict.

    Raises UpdateError and puts the folder back if a write fails halfway.
    """
    prefix, files = _zip_files(data)
    folder_sha = _sha_from_prefix(prefix)
    if folder_sha:
        commit = folder_sha
    ops = []
    for rel, raw in files:
        rel = rel.replace("\\", "/")
        if is_preserved(rel):
            continue
        target = safe_target(root, rel)
        existed = target.is_file()
        if target.exists() and not target.is_file():
            raise UpdateError(f"Refusing to replace {rel}: it is not a file.")
        new_bytes = raw
        keys: list[str] = []
        if rel == "config.yaml" and existed:
            try:
                merged, keys = merge_config_text(
                    target.read_text(encoding="utf-8"),
                    raw.decode("utf-8"),
                )
            except UnicodeError:
                raise UpdateError("config.yaml in the update is not valid UTF-8.")
            new_bytes = merged.encode("utf-8")
        old_bytes = target.read_bytes() if existed else None
        if existed and old_bytes == new_bytes:
            continue
        ops.append({"rel": rel, "target": target, "data": new_bytes, "existed": existed,
                    "old": old_bytes, "keys": keys})
    if not ops and not any(rel == "VERSION" for rel, _ in files):
        # A zip of identical files is still a successful no-op if something was there.
        if not files:
            raise UpdateError("The download did not contain any project files.")

    previous_version = read_version(root)
    new_version = previous_version
    for rel, raw in files:
        if rel.replace("\\", "/") == "VERSION":
            text = raw.decode("utf-8", "replace").strip().splitlines()
            line = text[0].strip() if text else ""
            if _VERSION_RE.fullmatch(line):
                new_version = line
            break
    # version may also be produced by a merged write; ops win if present
    for op in ops:
        if op["rel"] == "VERSION":
            text = op["data"].decode("utf-8", "replace").strip().splitlines()
            line = text[0].strip() if text else ""
            if _VERSION_RE.fullmatch(line):
                new_version = line

    old_req = None
    new_req = None
    req_path = root / "requirements.txt"
    if req_path.is_file():
        old_req = req_path.read_bytes()
    for rel, raw in files:
        if rel.replace("\\", "/") == "requirements.txt" and not is_preserved(rel):
            new_req = raw
    for op in ops:
        if op["rel"] == "requirements.txt":
            new_req = op["data"]
    requirements_changed = _norm_newlines(old_req) != _norm_newlines(new_req) and new_req is not None

    meta_path = root / "state" / "update.json"
    had_meta = meta_path.is_file()
    old_meta = meta_path.read_bytes() if had_meta else None

    bid = _backup_id(now)
    bdir = root / "backups" / bid
    n = 2
    while bdir.exists():
        bdir = root / "backups" / f"{bid}-{n}"
        n += 1
    bid = bdir.name
    replaced: list[str] = []
    added: list[str] = []
    config_keys: list[str] = []
    try:
        bdir.mkdir(parents=True, exist_ok=False)
        if had_meta and old_meta is not None:
            meta_backup = bdir / "state" / "update.json"
            meta_backup.parent.mkdir(parents=True, exist_ok=True)
            meta_backup.write_bytes(old_meta)
        for op in ops:
            if op["existed"]:
                dest = bdir / op["rel"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(op["old"])
                replaced.append(op["rel"])
            _atomic_write(op["target"], op["data"])
            if not op["existed"]:
                added.append(op["rel"])
            config_keys.extend(op["keys"])
        manifest = {
            "id": bid,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "from_version": previous_version,
            "to_version": new_version,
            "from_commit": (read_installed(root).get("commit") if had_meta else None),
            "to_commit": commit,
            "replaced": replaced,
            "added": added,
            "requirements_changed": requirements_changed,
            "config_keys_added": config_keys,
            "had_update_meta": had_meta,
        }
        # from_commit must be the pre-update value. read_installed now still sees old file.
        if had_meta:
            try:
                manifest["from_commit"] = json.loads(old_meta.decode("utf-8")).get("commit")
            except (UnicodeError, json.JSONDecodeError):
                manifest["from_commit"] = None
        (bdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _write_installed(root, new_version, commit)
        manifest["backup_dir"] = str(bdir)
        return manifest
    except Exception as e:
        for rel in reversed(replaced):
            src = bdir / rel
            if src.is_file():
                _atomic_write(safe_target(root, rel), src.read_bytes())
        for rel in added:
            try:
                target = safe_target(root, rel)
            except UpdateError:
                continue
            if target.is_file():
                target.unlink()
        if had_meta and old_meta is not None:
            _atomic_write(meta_path, old_meta)
        elif meta_path.is_file() and not had_meta:
            meta_path.unlink()
        shutil.rmtree(bdir, ignore_errors=True)
        if isinstance(e, UpdateError):
            raise
        raise UpdateError(redact(f"Update failed and was rolled back: {e}")) from None


def list_backups(root: Path) -> list[dict]:
    base = root / "backups"
    if not base.is_dir():
        return []
    found = []
    for directory in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        found.append({
            "id": directory.name,
            "created": data.get("created"),
            "from_version": data.get("from_version"),
            "to_version": data.get("to_version"),
            "to_commit": data.get("to_commit"),
            "from_commit": data.get("from_commit"),
        })
    return found


def _backup_dir(root: Path, backup_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", backup_id or ""):
        raise UpdateError("Unknown backup.")
    base = (root / "backups").resolve()
    directory = (base / backup_id).resolve()
    if directory.parent != base or not directory.is_dir():
        raise UpdateError("That backup no longer exists.")
    return directory


def rollback(root: Path, backup_id: str | None = None, *, status_fn=None, pip_runner=None,
             on_progress=None) -> ApplyResult:
    """Restore files from a backup. .env, logs, .venv and the rest of state stay."""
    bot = _guard(status_fn)
    backups = list_backups(root)
    if not backups:
        raise UpdateError("There is no backup to roll back to.")
    bid = backup_id or backups[0]["id"]
    directory = _backup_dir(root, bid)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        raise UpdateError("That backup is missing its manifest.")
    if not isinstance(manifest, dict):
        raise UpdateError("That backup is missing its manifest.")
    if on_progress:
        on_progress(f"Restoring backup {bid}…")
    replaced = [r for r in manifest.get("replaced") or [] if isinstance(r, str)]
    added = [r for r in manifest.get("added") or [] if isinstance(r, str)]
    for rel in replaced + added:
        if is_preserved(rel) and rel != "state/update.json":
            raise UpdateError(f"Backup manifest has an unexpected path: {rel}")
        safe_target(root, rel)
    for rel in replaced:
        src = directory / rel
        if not src.is_file():
            raise UpdateError(f"Backup is missing {rel}")
        _atomic_write(safe_target(root, rel), src.read_bytes())
    for rel in added:
        if is_preserved(rel):
            continue
        target = safe_target(root, rel)
        if target.is_file():
            target.unlink()
    meta_src = directory / "state" / "update.json"
    meta_path = root / "state" / "update.json"
    if meta_src.is_file():
        _atomic_write(meta_path, meta_src.read_bytes())
    elif not manifest.get("had_update_meta") and meta_path.is_file():
        meta_path.unlink()
    pip_ok = None
    pip_output = ""
    if manifest.get("requirements_changed"):
        if on_progress:
            on_progress("requirements.txt was restored. Running pip install…")
        pip_ok, pip_output = (pip_runner or default_pip)(root)
    version = read_version(root)
    lines = [
        f"Rolled back to v{manifest.get('from_version') or version} using backup {bid}.",
        "Your .env, logs, state and .venv were left in place.",
    ]
    if manifest.get("requirements_changed"):
        lines.append("")
        lines.append("pip install " + ("succeeded." if pip_ok else "failed:"))
        if pip_output:
            lines.append(_clip(pip_output))
    if bot.get("alive"):
        lines.append("")
        lines.append("The bot is still running the previous code. Stop it and start it again.")
    lines.append("")
    lines.append(_restart_hint(ok=pip_ok is not False))
    ok = pip_ok is not False
    summary = lines[0] if ok else "Files were restored, but pip install failed."
    return ApplyResult(ok=ok, summary=summary, output="\n".join(lines).strip(), version=version,
                       commit=(read_installed(root).get("commit") if isinstance(read_installed(root).get("commit"), str) else None),
                       backup_id=bid, requirements_changed=bool(manifest.get("requirements_changed")),
                       pip_ok=pip_ok, files_updated=replaced, files_added=[])


def default_pip(root: Path) -> tuple[bool, str]:
    """pip install -r requirements.txt with this same Python."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + "\n" + (e.stderr or "")).strip()
        return False, (out + "\npip timed out after 10 minutes.").strip()
    except OSError as e:
        return False, f"Could not run pip: {e}"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode == 0, out.strip()


def _clip(text: str, limit: int = 20000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n… (output truncated)"


def _restart_hint(ok: bool = True) -> str:
    if not ok:
        return ("Fix the pip error above, or roll back. Then restart the dashboard "
                "(on Windows, close the window and double-click start_dashboard.bat).")
    if os.name == "nt":
        return ("Restart the dashboard to load the new files. On Windows it will try to "
                "reopen itself; if this page does not come back, double-click start_dashboard.bat.")
    return "Restart the dashboard to load the new files (stop this process and start it again)."


def _guard(status_fn) -> dict:
    bot = status_fn() if status_fn else {}
    if not isinstance(bot, dict):
        bot = {}
    reason = trading_block_reason(bot)
    if reason:
        raise UpdateError(reason)
    return bot


def _format_apply(manifest: dict, *, pip_ok: bool | None, pip_output: str, bot: dict) -> ApplyResult:
    version = manifest.get("to_version") or ""
    previous = manifest.get("from_version") or "unknown"
    commit = manifest.get("to_commit")
    keys = manifest.get("config_keys_added") or []
    updated = manifest.get("replaced") or []
    added = manifest.get("added") or []
    req = bool(manifest.get("requirements_changed"))
    short = (commit or "")[:12]
    lines = [f"Updated v{previous} → v{version}" + (f" ({short})" if short else "") + "."]
    lines.append(f"Backup: backups/{manifest.get('id')}")
    if updated:
        shown = ", ".join(updated[:40])
        extra = f" and {len(updated) - 40} more" if len(updated) > 40 else ""
        lines.append("Replaced: " + shown + extra)
    if added:
        shown = ", ".join(added[:40])
        lines.append("Added: " + shown)
    if keys:
        lines.append("config.yaml: kept your values and added " + ", ".join(keys[:30]))
    else:
        lines.append("config.yaml: your values were kept.")
    lines.append("Left untouched: .env, logs/, state/, .venv/.")
    if req:
        lines.append("")
        lines.append("requirements.txt changed. pip install " + ("succeeded." if pip_ok else "FAILED:"))
        if pip_output:
            lines.append(_clip(pip_output))
    if bot.get("alive"):
        lines.append("")
        lines.append("The bot process is still running the old code. Stop it and start it again.")
    lines.append("")
    lines.append(_restart_hint(ok=pip_ok is not False))
    ok = pip_ok is not False
    summary = lines[0]
    if not ok:
        summary = f"Files updated to v{version}, but pip install failed. You can roll back."
    return ApplyResult(
        ok=ok, summary=summary, output="\n".join(lines).strip(), version=version, commit=commit,
        backup_id=manifest.get("id"), requirements_changed=req, pip_ok=pip_ok,
        files_updated=list(updated), files_added=list(added), config_keys_added=list(keys),
    )


def perform_update(root: Path, *, token: str | None = None, fetch=None, status_fn=None,
                   pip_runner=None, on_progress=None) -> ApplyResult:
    """Download main's zipball and apply it. `fetch` is injectable for tests."""
    _guard(status_fn)
    if fetch is None:
        tok = token

        def fetch(url, _token=tok):
            limit = 80_000_000 if "/zipball/" in url else 2_000_000
            timeout = 90 if "/zipball/" in url else 25
            return http_get(url, token=_token, timeout=timeout, max_bytes=limit)

    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    progress("Checking GitHub…")
    info = check_for_updates(root, token=token, fetch=fetch)
    if info.get("error"):
        raise UpdateError(info["error"])
    if not info.get("update_available"):
        raise UpdateError(f"Already up to date (v{info.get('local_version')}).")
    progress("Downloading the main branch…")
    package = fetch(f"{API_ROOT}/zipball/main")
    data = package.body
    if not data.startswith(b"PK"):
        raise UpdateError("GitHub did not return a zip archive of main.")
    bot = _guard(status_fn)
    progress("Backing up this folder and updating files…")
    manifest = apply_zip_bytes(root, data, commit=info.get("remote_commit"))
    pip_ok = None
    pip_output = ""
    if manifest.get("requirements_changed"):
        progress("requirements.txt changed. Running pip install…\n")
        pip_ok, pip_output = (pip_runner or default_pip)(root)
        progress("requirements.txt changed. Running pip install…\n" + _clip(pip_output, 8000))
    return _format_apply(manifest, pip_ok=pip_ok, pip_output=pip_output, bot=bot)
