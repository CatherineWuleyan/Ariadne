"""Small shared utilities: paths, env, subprocess logging, run directories."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO = Path(__file__).resolve().parents[1]
PRODUCT = REPO / "ariadne"
DEFAULT_CONFIG = PRODUCT / "configs" / "aircraft.demo.json"
VENV_PY = REPO / ".venv-gaia-a7" / "bin" / "python"
HARNESS_PATH = ":".join(str(p) for p in (
    REPO, REPO / "agent", REPO / "pipelines" / "single_paper", REPO / "pipelines" / "merge",
))


class AriadneError(RuntimeError):
    """A user-facing failure with a concrete next step."""


def python_bin() -> str:
    explicit = os.environ.get("ARIADNE_PYTHON")
    if explicit:
        return explicit
    if VENV_PY.is_file():
        return str(VENV_PY)
    return sys.executable


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Read a dotenv-style file without mutating the process environment.

    Supports optional ``export`` prefixes and simple single/double quoting.
    Missing files return an empty mapping; malformed lines are ignored.
    """
    values: dict[str, str] = {}
    if path is None or not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and value:
            values[key] = value
    return values


def load_env(path: Path | None = None) -> None:
    """Load repo-local keys without overwriting an explicit environment."""
    for key, value in read_env_file(path or (REPO / "agent" / ".env")).items():
        os.environ.setdefault(key, value)


def slugify(value: str, limit: int = 42) -> str:
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value.strip()).strip("-")
    return (text[:limit] or "question").strip("-")


def new_run_dir(question: str, out: Path | None = None) -> Path:
    if out is not None:
        # Keep every downstream subprocess path absolute.  A relative --out
        # would otherwise be joined twice once the child runs with cwd=stage.
        out = Path(out).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
        return out
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPO / "outputs" / "ariadne_runs" / f"run_{stamp}_{slugify(question)}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def harness_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = HARNESS_PATH + ((":" + existing) if existing else "")
    if extra:
        env.update(extra)
    return env


SECRET_FLAGS = frozenset({
    "--ak", "--access-key", "--api-key", "--token", "--password", "--secret",
})


def redact_argv(argv: Iterable[str]) -> list[str]:
    """Hide values that follow a known secret flag in logs and errors."""
    redacted: list[str] = []
    hide_next = False
    for item in argv:
        text = str(item)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(text)
        if text in SECRET_FLAGS:
            hide_next = True
        elif "=" in text and text.split("=", 1)[0] in SECRET_FLAGS:
            redacted[-1] = text.split("=", 1)[0] + "=<redacted>"
    return redacted


def run(
    cmd: Iterable[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    log: Path | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    argv = [str(item) for item in cmd]
    # Child commands must never inherit an interactive stdin.  A missing
    # confirmation flag then fails fast instead of hanging a batch run.
    proc = subprocess.run(
        argv, cwd=str(cwd) if cwd else None, env=dict(env) if env else None,
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "$ " + " ".join(redact_argv(argv)) + "\n\n[stdout]\n" + proc.stdout +
            "\n[stderr]\n" + proc.stderr,
            encoding="utf-8",
        )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1200:]
        raise AriadneError(
            f"command failed ({proc.returncode}): {' '.join(redact_argv(argv))}\n{tail}"
        )
    return proc


def run_interactive(
    cmd: Iterable[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a command with inherited stdio, for interactive logins only."""
    argv = [str(item) for item in cmd]
    proc = subprocess.run(argv, cwd=str(cwd) if cwd else None,
                          env=dict(env) if env else None)
    return int(proc.returncode)


def which(name: str) -> str | None:
    return shutil.which(name)


def git_state(root: Path | None = None) -> dict[str, Any]:
    """Identify the code that produced a run.

    A run.json without this cannot answer "why did the graph change?" once the
    checkout moves on. Missing git (a tarball install) is reported, not guessed.
    """
    base = str(root or REPO)
    state: dict[str, Any] = {"available": False, "commit": None, "dirty": None, "describe": None}
    try:
        head = subprocess.run(
            ["git", "-C", base, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if head.returncode != 0:
            return state
        state["commit"] = head.stdout.strip()
        status = subprocess.run(
            ["git", "-C", base, "status", "--porcelain"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        state["dirty"] = bool(status.stdout.strip()) if status.returncode == 0 else None
        describe = subprocess.run(
            ["git", "-C", base, "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        state["describe"] = describe.stdout.strip() if describe.returncode == 0 else None
        state["available"] = True
    except (OSError, subprocess.SubprocessError):
        return state
    return state


DEFAULT_WORKERS = 4


def parallel_workers(explicit: int | None = None, ceiling: int | None = None) -> int:
    """How many independent jobs to run at once.

    Papers, queries and extractions are independent; running them one at a time
    was the dominant cost. ``ARIADNE_WORKERS`` tunes it without a code change,
    and ``ceiling`` keeps small batches from starting idle workers.
    """
    raw: int | None = explicit
    if raw is None:
        env = os.environ.get("ARIADNE_WORKERS", "").strip()
        raw = int(env) if env.isdigit() else DEFAULT_WORKERS
    workers = max(1, int(raw))
    if ceiling is not None:
        workers = min(workers, max(1, int(ceiling)))
    return workers


def map_ordered(func: Any, items: Iterable[Any], workers: int | None = None
                ) -> tuple[list[Any], list[tuple[Any, BaseException]]]:
    """Apply ``func`` to every item, optionally at the same time.

    Returns ``(results, failures)`` with results in the input order, so a caller
    that fans out independent work still produces deterministic output. Failures
    are returned instead of raised: one bad paper or query must not abort the
    batch. ``workers <= 1`` runs the plain sequential path.
    """
    ordered = list(items)
    count = parallel_workers(workers, ceiling=len(ordered))
    results: list[Any] = []
    failures: list[tuple[Any, BaseException]] = []
    if count <= 1 or len(ordered) <= 1:
        for item in ordered:
            try:
                results.append(func(item))
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                failures.append((item, exc))
        return results, failures
    completed: list[tuple[int, Any]] = []
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = {pool.submit(func, item): index for index, item in enumerate(ordered)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                completed.append((index, future.result()))
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                failures.append((ordered[index], exc))
    results = [result for _, result in sorted(completed)]
    return results, failures


def file_digest(path: Path) -> str | None:
    """sha256 of a file, or None when it is absent."""
    if not path.is_file():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def stage_log(run_dir: Path, name: str) -> Path:
    return run_dir / "logs" / f"{name}.log"


def fail(message: str) -> None:
    raise AriadneError(message)
