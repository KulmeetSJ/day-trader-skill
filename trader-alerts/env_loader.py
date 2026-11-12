"""Safe, optional .env loader using Python standard library only.

Security and compatibility guarantees:
- Locates .env relative to the project root (or enclosing repo root for in-repo dist/ packages), never the current working directory.
- Supports KEY=value, optional `export KEY=value`, single-quoted literals, and double-quoted strings.
- Never executes shell commands, subprocesses, eval/exec, or variable interpolation ($VAR, ${VAR}, $(...), `...`).
- Never prints or logs loaded values or secrets.
- Never overwrites existing process environment variables by default.
- Missing or empty .env files are ignored so zero-credential Yahoo workflows stay unbroken.
"""

import os
import re

_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_project_root(project_root=None):
    """Return the repository/plugin root directory, independent of os.getcwd()."""
    if project_root is not None:
        return os.path.abspath(str(project_root))
    return PROJECT_ROOT


def _parse_double_quoted(raw_val):
    """Parse a double-quoted dotenv value without variable interpolation."""
    out = []
    i = 1
    n = len(raw_val)
    while i < n:
        ch = raw_val[i]
        if ch == '"':
            trailing = raw_val[i + 1 :].strip()
            if not trailing or trailing.startswith("#"):
                return "".join(out), True
            return None, False
        if ch == "\\" and i + 1 < n:
            nxt = raw_val[i + 1]
            if nxt == "n":
                out.append("\n")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "t":
                out.append("\t")
            elif nxt in ('"', "\\"):
                out.append(nxt)
            else:
                out.append("\\")
                out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return None, False


def _parse_single_quoted(raw_val):
    """Parse a single-quoted POSIX-literal dotenv value."""
    end_idx = raw_val.find("'", 1)
    if end_idx == -1:
        return None, False
    trailing = raw_val[end_idx + 1 :].strip()
    if trailing and not trailing.startswith("#"):
        return None, False
    return raw_val[1:end_idx], True


def _parse_unquoted(raw_val):
    """Parse an unquoted dotenv value, trimming inline comments preceded by whitespace."""
    if not raw_val:
        return ""
    if raw_val.startswith("#"):
        return ""
    for idx, ch in enumerate(raw_val):
        if ch == "#" and idx > 0 and raw_val[idx - 1].isspace():
            return raw_val[:idx].strip()
    return raw_val.strip()


def parse_dotenv(content):
    """Parse .env text into an ordered dict of {KEY: value} without interpolation or shell execution."""
    if not isinstance(content, str):
        return {}
    if content.startswith("\ufeff"):
        content = content[1:]
    parsed = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        raw_val = match.group(2).lstrip()
        if not raw_val:
            parsed[key] = ""
            continue
        if raw_val.startswith('"'):
            val, ok = _parse_double_quoted(raw_val)
            if ok:
                parsed[key] = val
            continue
        if raw_val.startswith("'"):
            val, ok = _parse_single_quoted(raw_val)
            if ok:
                parsed[key] = val
            continue
        parsed[key] = _parse_unquoted(raw_val)
    return parsed


def load_dotenv_file(path, environ=None, override=False):
    """Load key/value pairs from a specific .env file path into environ.

    Existing environment variables are never overwritten unless override=True.
    Returns a tuple of loaded key names (never values). Missing or unreadable files return ().
    """
    target = os.environ if environ is None else environ
    if not path or not os.path.isfile(path):
        return ()
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
    except OSError:
        return ()
    pairs = parse_dotenv(content)
    loaded = []
    for key, val in pairs.items():
        if override or key not in target:
            target[key] = val
            loaded.append(key)
    return tuple(loaded)


def _find_packaged_repo_dotenv(pkg_root):
    """Find <repo>/.env ONLY when pkg_root is an in-repo dist/ package copy.

    Never resolves via os.getcwd() and stops within a bounded parent depth so
    standalone extractions (such as Claude Chat containers) never search parent
    directories arbitrarily.
    """
    abs_root = os.path.abspath(str(pkg_root))
    cur = abs_root
    for _ in range(4):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        rel_parts = os.path.relpath(abs_root, parent).split(os.sep)
        if rel_parts and rel_parts[0] == "dist":
            if os.path.isfile(os.path.join(parent, "trader-alerts", "env_loader.py")):
                cand_env = os.path.join(parent, ".env")
                if os.path.isfile(cand_env):
                    return cand_env
        cur = parent
    return None


def load_project_dotenv(project_root=None, environ=None, override=False):
    """Load <project_root>/.env (or enclosing repo .env for in-repo dist/) without overwriting existing env vars."""
    root = resolve_project_root(project_root)
    env_path = os.path.join(root, ".env")
    if project_root is None and not os.path.isfile(env_path):
        packaged_env = _find_packaged_repo_dotenv(root)
        if packaged_env:
            env_path = packaged_env
    return load_dotenv_file(env_path, environ=environ, override=override)

