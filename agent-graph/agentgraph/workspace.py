"""Git worktree per job, plus the deterministic checks that prove a code loop is done."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from .config import HARD, PROTECTED_PATHS

DOC_FILES = {"DOCS.md", "README.md"}


class WorkspaceError(Exception):
    pass


def _git_env() -> dict:
    """Auth header through env, so the token never shows in argv, remotes or logs."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    return env


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, text=True, timeout=300)
    if check and r.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed: {r.stderr.strip()[:500]}")
    return r.stdout


class Workspace:
    def __init__(self, home: Path, repo: str, base: str, remote_url: str | None = None):
        self.home, self.repo, self.base = Path(home), repo, base
        self.mirror = self.home / "repo"
        self.remote_url = remote_url or f"https://github.com/{repo}.git"

    def ensure_clone(self) -> None:
        if not (self.mirror / ".git").exists():
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            git(self.mirror.parent, "clone", "--quiet", self.remote_url, self.mirror.name)
        git(self.mirror, "fetch", "--quiet", "origin", self.base)

    def addons(self) -> dict[str, None]:
        """Option set for the `addon` question, built by code from the repo, never by a model."""
        self.ensure_clone()
        names = git(self.mirror, "ls-tree", "-r", "--name-only", f"origin/{self.base}").splitlines()
        return {n.split("/")[0]: None for n in sorted(names) if n.count("/") == 1 and n.endswith("/config.yaml")}

    def worktree(self, issue: int) -> tuple[Path, str]:
        branch = f"agent/issue-{issue}"
        path = self.home / "work" / f"issue-{issue}"
        if path.exists():
            git(self.mirror, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
        git(self.mirror, "worktree", "prune")
        git(self.mirror, "worktree", "add", "--quiet", "-B", branch, str(path), f"origin/{self.base}")
        return path, branch

    @staticmethod
    def listing(wt: Path, addon: str) -> list[str]:
        root = wt / addon
        return sorted(str(p.relative_to(wt)) for p in root.rglob("*") if p.is_file() and p.suffix not in (".png", ".jpg"))

    @staticmethod
    def read(wt: Path, paths: list[str], limit: int = 40_000) -> dict[str, str]:
        out = {}
        for p in paths:
            f = wt / p
            if f.is_file():
                out[p] = f.read_text(errors="replace")[:limit]
        return out

    @staticmethod
    def safe_path(wt: Path, addon: str, rel: str) -> Path:
        """Reject traversal, protected paths and anything outside the chosen add-on."""
        if rel.startswith("/") or ".." in Path(rel).parts or any(rel.startswith(p) for p in PROTECTED_PATHS):
            raise WorkspaceError(f"path not allowed: {rel}")
        if Path(rel).parts[:1] != (addon,):
            raise WorkspaceError(f"path outside add-on {addon}: {rel}")
        full = (wt / rel).resolve()
        if not str(full).startswith(str((wt / addon).resolve()) + os.sep):
            raise WorkspaceError(f"path escapes add-on: {rel}")
        return full

    def apply(self, wt: Path, addon: str, files: list[dict]) -> list[str]:
        if not files:
            raise WorkspaceError("patch has no files")
        if len(files) > HARD["max_files_per_diff"]:
            raise WorkspaceError(f"patch has {len(files)} files, cap is {HARD['max_files_per_diff']}")
        written = []
        for f in files:
            if len(f["content"].encode()) > HARD["max_file_bytes"]:
                raise WorkspaceError(f"{f['path']} over {HARD['max_file_bytes']} bytes")
            full = self.safe_path(wt, addon, f["path"])
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(f["content"])
            written.append(f["path"])
        return written

    @staticmethod
    def diff(wt: Path) -> str:
        git(wt, "add", "-A")
        d = git(wt, "diff", "--cached", "--no-color")
        if len(d.encode()) > HARD["max_diff_bytes"]:
            raise WorkspaceError(f"diff over {HARD['max_diff_bytes']} bytes")
        return d

    @staticmethod
    def changed(wt: Path) -> list[str]:
        git(wt, "add", "-A")
        return [p for p in git(wt, "diff", "--cached", "--name-only").splitlines() if p]

    def commit_push(self, wt: Path, branch: str, message: str) -> str:
        git(wt, "-c", "user.name=agent-graph", "-c", "user.email=agent-graph@users.noreply.github.com",
            "commit", "--quiet", "-m", message)
        git(wt, "push", "--quiet", "--force-with-lease", "origin", f"{branch}:{branch}")
        return git(wt, "rev-parse", "HEAD").strip()


# ---------- deterministic checks: the proof a code loop is done ----------

def _version(text: str) -> str | None:
    try:
        return str((yaml.safe_load(text) or {}).get("version"))
    except yaml.YAMLError:
        return None


def run_checks(wt: Path, addon: str, base_ref: str) -> list[str]:
    """Return failures. Empty list means every check passed."""
    fails: list[str] = []
    changed = Workspace.changed(wt)
    if not changed:
        return ["no files changed"]
    for p in changed:
        if Path(p).parts[:1] != (addon,):
            fails.append(f"scope: {p} is outside {addon}")
    for p in changed:
        f = wt / p
        if not f.exists():
            continue
        if f.suffix in (".yaml", ".yml"):
            try:
                yaml.safe_load(f.read_text())
            except yaml.YAMLError as e:
                fails.append(f"yaml parse {p}: {str(e)[:200]}")
        if f.suffix == ".json":
            try:
                json.loads(f.read_text())
            except ValueError as e:
                fails.append(f"json parse {p}: {e}")
    yamls = [p for p in changed if p.endswith((".yaml", ".yml")) and (wt / p).exists()]
    if yamls and shutil.which("yamllint"):
        r = subprocess.run(["yamllint", "-f", "parsable", *yamls], cwd=wt, capture_output=True, text=True)
        errs = [ln for ln in r.stdout.splitlines() if "[error]" in ln]
        fails += [f"yamllint {ln}" for ln in errs[:10]]
    shells = [p for p in changed if (wt / p).is_file() and _is_shell(wt / p)]
    if shells and shutil.which("shellcheck"):
        r = subprocess.run(["shellcheck", "-e", "SC1008", "-s", "bash", "-f", "gcc", *shells], cwd=wt, capture_output=True, text=True)
        fails += [f"shellcheck {ln}" for ln in r.stdout.splitlines()[:10] if ": error:" in ln or ": warning:" in ln]
    non_doc = [p for p in changed if Path(p).name not in DOC_FILES and "translations" not in Path(p).parts
               and Path(p).name != "CHANGELOG.md"]
    if non_doc:
        cfg = wt / addon / "config.yaml"
        old = git(wt, "show", f"{base_ref}:{addon}/config.yaml", check=False)
        new_v, old_v = _version(cfg.read_text()) if cfg.exists() else None, _version(old) if old else None
        if not new_v or new_v == old_v:
            fails.append(f"version: {addon}/config.yaml version not bumped (still {old_v})")
        else:
            log = wt / addon / "CHANGELOG.md"
            head = log.read_text()[:2000] if log.exists() else ""
            if not re.search(rf"^##\s*{re.escape(new_v)}\b", head, re.M):
                fails.append(f"changelog: {addon}/CHANGELOG.md has no '## {new_v}' entry at the top")
    return fails


def _is_shell(f: Path) -> bool:
    if f.suffix == ".sh":
        return True
    try:
        first = f.open("rb").readline(100)
    except OSError:
        return False
    return first.startswith(b"#!") and (b"bash" in first or b"/sh" in first or b"bashio" in first)
