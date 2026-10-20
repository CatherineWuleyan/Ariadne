"""User configuration and runtime resolution for Ariadne.

Secrets live in one JSON file outside the repository, by default:

    $ARIADNE_CONFIG
    $XDG_CONFIG_HOME/ariadne/config.json
    ~/.config/ariadne/config.json

The file is written with mode ``0600``.  Environment variables always win over
the file, so CI and one-off runs do not need to edit it:

    ARIADNE_MODEL_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY
    ARIADNE_MODEL_BASE_URL / DEEPSEEK_BASE_URL / OPENAI_BASE_URL
    ARIADNE_MODEL / DEEPSEEK_MODEL / OPENAI_MODEL
    ARIADNE_MERGE_MODEL / DEEPSEEK_MERGE_MODEL
    ARIADNE_BOHR_ACCESS_KEY / BOHR_ACCESS_KEY
    ARIADNE_BOHR_PROFILE
    ARIADNE_PYTHON

The legacy ``agent/.env`` file is still read, but only as the lowest-precedence
source.  Nothing in this module prints a secret.
"""
from __future__ import annotations

import copy
import json
import os
import stat as stat_module
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .util import (
    AriadneError,
    REPO,
    VENV_PY,
    read_env_file,
    read_json,
    run,
    which,
)

CONFIG_ENV = "ARIADNE_CONFIG"
AGENT_ENV = REPO / "agent" / ".env"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BOHR_HOST = "https://open.bohrium.com"

DEFAULTS: dict[str, Any] = {
    "version": 1,
    "model": {
        "provider": "deepseek",
        "api_key": "",
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "merge_model": DEFAULT_MODEL,
    },
    "bohr": {
        "profile": "",
        "access_key": "",
        "host": DEFAULT_BOHR_HOST,
        "auto_approve_spend": False,
        "payment_source": "",
    },
    "runtime": {
        "python": "",
    },
    "retrieval": {
        "use_query_planner": True,
        "queries": 5,
        "per_query": 8,
        "candidate_limit": 16,
        "min_papers": 2,
        "min_graph_nodes": 3,
        "min_relevance": 0.2,
        "use_lkm_extract": True,
        "max_extractions": 5,
    },
}

SECRET_KEYS = frozenset({"model.api_key", "bohr.access_key"})
BOOL_KEYS = frozenset({
    "bohr.auto_approve_spend",
    "retrieval.use_query_planner",
    "retrieval.use_lkm_extract",
})
INT_KEYS = frozenset({
    "retrieval.queries",
    "retrieval.per_query",
    "retrieval.candidate_limit",
    "retrieval.min_papers",
    "retrieval.min_graph_nodes",
    "retrieval.max_extractions",
})
FLOAT_KEYS = frozenset({"retrieval.min_relevance"})
INT_RANGES = {
    "retrieval.queries": (1, 8),
    "retrieval.per_query": (1, 50),
    "retrieval.candidate_limit": (2, 40),
    "retrieval.min_papers": (2, 20),
    "retrieval.min_graph_nodes": (1, 100),
    "retrieval.max_extractions": (0, 10),
}
KNOWN_KEYS = frozenset({
    "model.provider",
    "model.api_key",
    "model.base_url",
    "model.model",
    "model.merge_model",
    "bohr.profile",
    "bohr.access_key",
    "bohr.host",
    "bohr.auto_approve_spend",
    "bohr.payment_source",
    "runtime.python",
    "retrieval.use_query_planner",
    "retrieval.queries",
    "retrieval.per_query",
    "retrieval.candidate_limit",
    "retrieval.min_papers",
    "retrieval.min_graph_nodes",
    "retrieval.min_relevance",
    "retrieval.use_lkm_extract",
    "retrieval.max_extractions",
})
PROVIDERS = frozenset({"deepseek", "openai-compatible"})
PAYMENT_SOURCES = frozenset({"", "photon", "balance"})


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def default_config_path() -> Path:
    explicit = os.environ.get(CONFIG_ENV)
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        root = os.environ.get("APPDATA")
        if root:
            return Path(root) / "ariadne" / "config.json"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "ariadne" / "config.json"


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    raise AriadneError(f"expected a boolean, got {value!r}")


def mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "********"
    return f"{text[:4]}…{text[-4:]}"


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), Mapping):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _lookup(data: Mapping[str, Any], key: str) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _flatten_keys(data: Mapping[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            keys.extend(_flatten_keys(value, full))
        else:
            keys.append(full)
    return keys


def file_mode(path: Path) -> int | None:
    try:
        return stat_module.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def mode_is_private(path: Path) -> bool:
    mode = file_mode(path)
    return mode is not None and (mode & 0o077) == 0


# --------------------------------------------------------------------------- #
# configuration file
# --------------------------------------------------------------------------- #

class Config:
    """A parsed config file merged over defaults."""

    def __init__(self, data: dict[str, Any], path: Path, *, exists: bool,
                 explicit: set[str] | None = None) -> None:
        self.data = data
        self.path = path
        self.exists = exists
        self.explicit = set(explicit or ())

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        target = Path(path).expanduser() if path else default_config_path()
        exists = target.is_file()
        data = copy.deepcopy(DEFAULTS)
        if exists:
            try:
                raw = read_json(target)
            except json.JSONDecodeError as exc:
                raise AriadneError(
                    f"invalid JSON in {target}: {exc.msg} (line {exc.lineno})"
                ) from exc
            except OSError as exc:
                raise AriadneError(f"cannot read {target}: {exc}") from exc
            if not isinstance(raw, Mapping):
                raise AriadneError(f"{target} must contain a JSON object")
            _deep_merge(data, raw)
            explicit = set(_flatten_keys(raw))
        else:
            explicit = set()
        return cls(data, target, exists=exists, explicit=explicit)

    def get(self, key: str, default: Any = None) -> Any:
        value = _lookup(self.data, key)
        return copy.deepcopy(default) if value is None else value

    def get_explicit(self, key: str, default: Any = None) -> Any:
        """Return a value only when it is actually present in the file."""
        if key not in self.explicit:
            return copy.deepcopy(default)
        return self.get(key, default)

    def set(self, key: str, value: Any) -> Any:
        if key not in KNOWN_KEYS:
            raise AriadneError(
                f"unknown configuration key: {key}\nknown keys: {', '.join(sorted(KNOWN_KEYS))}"
            )
        coerced = coerce_value(key, value)
        section, name = key.split(".", 1)
        self.data.setdefault(section, {})[name] = coerced
        self.explicit.add(key)
        return coerced

    def unset(self, key: str) -> None:
        if key not in KNOWN_KEYS:
            raise AriadneError(f"unknown configuration key: {key}")
        section, name = key.split(".", 1)
        self.data.setdefault(section, {})[name] = copy.deepcopy(
            DEFAULTS[section][name]
        )
        self.explicit.discard(key)

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path).expanduser() if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        sparse: dict[str, Any] = {"version": 1}
        for section, values in self.data.items():
            if section == "version" or not isinstance(values, Mapping):
                continue
            defaults = DEFAULTS.get(section)
            if not isinstance(defaults, Mapping):
                continue
            for name, value in values.items():
                key = f"{section}.{name}"
                if key in self.explicit or value != defaults.get(name):
                    sparse.setdefault(section, {})[name] = value
        payload = json.dumps(sparse, ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix=".config-", suffix=".json", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.path = target
        self.exists = True
        return target

    def masked(self) -> dict[str, Any]:
        data = copy.deepcopy(self.data)
        for key in SECRET_KEYS:
            section, name = key.split(".", 1)
            value = data.get(section, {}).get(name, "")
            data[section][name] = mask_secret(value)
        return data


def coerce_value(key: str, value: Any) -> Any:
    if key in BOOL_KEYS:
        return parse_bool(value)
    if key in INT_KEYS:
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise AriadneError(f"{key} must be an integer") from exc
        low, high = INT_RANGES[key]
        if not low <= number <= high:
            raise AriadneError(f"{key} must be between {low} and {high}")
        return number
    if key in FLOAT_KEYS:
        try:
            number = float(str(value).strip())
        except ValueError as exc:
            raise AriadneError(f"{key} must be a number") from exc
        if not 0.0 <= number <= 1.0:
            raise AriadneError(f"{key} must be between 0 and 1")
        return number
    if key == "model.provider":
        provider = str(value).strip().lower()
        if provider == "openai":
            provider = "openai-compatible"
        if provider not in PROVIDERS:
            raise AriadneError(
                "model.provider must be one of: " + ", ".join(sorted(PROVIDERS))
            )
        return provider
    if key == "model.base_url":
        text = str(value).strip().rstrip("/")
        if not text.startswith(("http://", "https://")):
            raise AriadneError("model.base_url must start with http:// or https://")
        return text
    if key == "bohr.payment_source":
        text = str(value).strip().lower()
        if text not in PAYMENT_SOURCES:
            raise AriadneError("bohr.payment_source must be empty, photon, or balance")
        return text
    if key == "runtime.python":
        text = str(value).strip()
        return str(Path(text).expanduser()) if text else ""
    if key in {"model.api_key", "bohr.access_key"}:
        return str(value).strip()
    return str(value).strip()


# --------------------------------------------------------------------------- #
# resolved runtime
# --------------------------------------------------------------------------- #

@dataclass
class Runtime:
    config: Config
    model_api_key: str = ""
    model_base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    merge_model: str = DEFAULT_MODEL
    bohr_profile: str = ""
    bohr_access_key: str = ""
    bohr_host: str = DEFAULT_BOHR_HOST
    auto_approve_spend: bool = False
    payment_source: str = ""
    python: str = ""
    bohr_bin: str = ""
    anthropic_key_present: bool = False
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def model_key_present(self) -> bool:
        return bool(self.model_api_key)

    @property
    def bohr_available(self) -> bool:
        return bool(self.bohr_bin)

    def apply_env(self) -> None:
        """Export the resolved values for the maintained pipeline scripts."""
        values = {
            "DEEPSEEK_API_KEY": self.model_api_key,
            "DEEPSEEK_BASE_URL": self.model_base_url,
            "DEEPSEEK_MODEL": self.model,
            "DEEPSEEK_MERGE_MODEL": self.merge_model,
            "ARIADNE_PYTHON": self.python,
            "ARIADNE_BOHR_PROFILE": self.bohr_profile,
            "ARIADNE_AUTO_APPROVE_SPEND": "1" if self.auto_approve_spend else "0",
            "ARIADNE_PAYMENT_SOURCE": self.payment_source,
        }
        for key, value in values.items():
            if value:
                os.environ[key] = str(value)
        if self.bohr_access_key:
            os.environ["BOHR_ACCESS_KEY"] = self.bohr_access_key
        if self.bohr_host:
            os.environ["BOHR_HOST"] = self.bohr_host

    def summary(self) -> dict[str, Any]:
        return {
            "config": {
                "path": str(self.config.path),
                "exists": self.config.exists,
            },
            "python": self.python,
            "model": {
                "provider": self.config.get("model.provider", "deepseek"),
                "base_url": self.model_base_url,
                "model": self.model,
                "merge_model": self.merge_model,
                "api_key_present": self.model_key_present,
                "api_key_source": self.sources.get("model_api_key", "missing"),
            },
            "bohr": {
                "cli": self.bohr_bin,
                "profile": self.bohr_profile,
                "host": self.bohr_host,
                "access_key_present": bool(self.bohr_access_key),
                "access_key_source": self.sources.get("bohr_access_key", "missing"),
                "auto_approve_spend": self.auto_approve_spend,
                "payment_source": self.payment_source,
            },
        }


def _pick(
    overrides: Mapping[str, Any],
    override_key: str,
    env: Mapping[str, str],
    env_keys: tuple[str, ...],
    config: Config,
    config_key: str,
    agent_env: Mapping[str, str],
    agent_keys: tuple[str, ...],
    default: str,
) -> tuple[str, str]:
    value = overrides.get(override_key)
    if value not in (None, ""):
        return str(value), "cli"
    for key in env_keys:
        if env.get(key):
            return str(env[key]), f"env:{key}"
    configured = config.get_explicit(config_key)
    if configured not in (None, ""):
        return str(configured), "config"
    for key in agent_keys:
        if agent_env.get(key):
            return str(agent_env[key]), f"agent/.env:{key}"
    return default, "default"


def resolve_runtime(
    config: Config | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    yes: bool = False,
    agent_env_path: Path | None = AGENT_ENV,
) -> Runtime:
    """Resolve one effective runtime from CLI > env > config > agent/.env."""
    config = config or Config.load()
    overrides = dict(overrides or {})
    env = os.environ
    agent_env = read_env_file(agent_env_path) if agent_env_path else {}

    model_key, model_key_source = _pick(
        overrides, "model_api_key", env,
        ("ARIADNE_MODEL_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        config, "model.api_key",
        agent_env, ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        "",
    )
    base_url, base_url_source = _pick(
        overrides, "model_base_url", env,
        ("ARIADNE_MODEL_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"),
        config, "model.base_url",
        agent_env, ("DEEPSEEK_BASE_URL", "OPENAI_BASE_URL"),
        DEFAULT_BASE_URL,
    )
    model, model_source = _pick(
        overrides, "model", env,
        ("ARIADNE_MODEL", "DEEPSEEK_MODEL", "OPENAI_MODEL"),
        config, "model.model",
        agent_env, ("DEEPSEEK_MODEL",),
        DEFAULT_MODEL,
    )
    merge_model, merge_model_source = _pick(
        overrides, "merge_model", env,
        ("ARIADNE_MERGE_MODEL", "DEEPSEEK_MERGE_MODEL"),
        config, "model.merge_model",
        agent_env, ("DEEPSEEK_MERGE_MODEL",),
        model,
    )
    bohr_profile, profile_source = _pick(
        overrides, "bohr_profile", env,
        ("ARIADNE_BOHR_PROFILE",),
        config, "bohr.profile",
        agent_env, (),
        "",
    )
    bohr_access_key, bohr_key_source = _pick(
        overrides, "bohr_access_key", env,
        ("ARIADNE_BOHR_ACCESS_KEY", "BOHR_ACCESS_KEY"),
        config, "bohr.access_key",
        agent_env, ("BOHR_ACCESS_KEY", "LKM_ACCESS_KEY"),
        "",
    )
    bohr_host, host_source = _pick(
        overrides, "bohr_host", env,
        ("ARIADNE_BOHR_HOST", "BOHR_HOST"),
        config, "bohr.host",
        agent_env, ("BOHR_HOST",),
        DEFAULT_BOHR_HOST,
    )
    payment_source, payment_source_source = _pick(
        overrides, "payment_source", env,
        ("ARIADNE_PAYMENT_SOURCE", "BOHR_PAYMENT_SOURCE"),
        config, "bohr.payment_source",
        agent_env, (),
        "",
    )
    python, python_source = _pick(
        overrides, "python", env,
        ("ARIADNE_PYTHON",),
        config, "runtime.python",
        agent_env, (),
        str(VENV_PY) if VENV_PY.is_file() else sys.executable,
    )
    if python and not Path(python).expanduser().is_file():
        discovered = which(python)
        if discovered:
            python = discovered
    else:
        python = str(Path(python).expanduser()) if python else python

    if base_url and not base_url.startswith(("http://", "https://")):
        raise AriadneError(f"invalid model base URL: {base_url}")
    if payment_source not in PAYMENT_SOURCES:
        raise AriadneError(
            f"invalid payment source {payment_source!r}; use photon, balance, or empty"
        )

    if yes:
        auto_approve, approve_source = True, "cli:--yes"
    elif "ARIADNE_AUTO_APPROVE_SPEND" in env:
        auto_approve = parse_bool(env["ARIADNE_AUTO_APPROVE_SPEND"])
        approve_source = "env:ARIADNE_AUTO_APPROVE_SPEND"
    else:
        auto_approve = parse_bool(config.get("bohr.auto_approve_spend", False))
        approve_source = "config"

    anthropic_present = bool(
        env.get("ANTHROPIC_API_KEY") or agent_env.get("ANTHROPIC_API_KEY")
    )
    return Runtime(
        config=config,
        model_api_key=model_key,
        model_base_url=base_url,
        model=model,
        merge_model=merge_model,
        bohr_profile=bohr_profile,
        bohr_access_key=bohr_access_key,
        bohr_host=bohr_host,
        auto_approve_spend=auto_approve,
        payment_source=payment_source,
        python=python,
        bohr_bin=which("bohr") or "",
        anthropic_key_present=anthropic_present,
        sources={
            "model_api_key": model_key_source,
            "model_base_url": base_url_source,
            "model": model_source,
            "merge_model": merge_model_source,
            "bohr_profile": profile_source,
            "bohr_access_key": bohr_key_source,
            "bohr_host": host_source,
            "payment_source": payment_source_source,
            "python": python_source,
            "auto_approve_spend": approve_source,
        },
    )


# --------------------------------------------------------------------------- #
# bohr helpers
# --------------------------------------------------------------------------- #

def bohr_command(runtime: Runtime, *args: str, yes: bool | None = None) -> list[str]:
    """Build a bohr invocation with the configured profile and billing flags."""
    if not runtime.bohr_bin:
        raise AriadneError(
            "bohr CLI not found. Install it with `npm install -g bohr-cli`, "
            "or use --papers with local graph/text files."
        )
    command = [runtime.bohr_bin]
    if runtime.bohr_profile:
        command += ["--profile", runtime.bohr_profile]
    command += list(args)
    approved = runtime.auto_approve_spend if yes is None else yes
    if approved:
        command += ["--yes"]
    if runtime.payment_source:
        command += ["--payment-source", runtime.payment_source]
    command += ["-o", "json"]
    return command


def _one_line(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bohr_error(text: str, selected: str) -> dict[str, Any]:
    message = ""
    try:
        payload = json.loads(text)
        message = (payload.get("error") or {}).get("message") or ""
    except (json.JSONDecodeError, AttributeError):
        message = text
    return {
        "available": True,
        "logged_in": False,
        "profile": selected,
        "error": _one_line(message) or "bohr auth status failed",
    }


def bohr_status(runtime: Runtime, profile: str | None = None) -> dict[str, Any]:
    """Return a small, secret-free view of ``bohr auth status``."""
    selected = profile or runtime.bohr_profile
    if not runtime.bohr_bin:
        return {"available": False, "logged_in": False, "profile": selected,
                "error": "bohr CLI not installed"}
    command = [runtime.bohr_bin, "auth", "status", "-o", "json"]
    if selected:
        command += ["--profile", selected]
    try:
        proc = run(command, check=False, timeout=30)
    except Exception as exc:  # noqa: BLE001 - status must never crash doctor
        return {"available": True, "logged_in": False, "profile": selected,
                "error": _one_line(exc)}
    if proc.returncode != 0:
        return _bohr_error(proc.stderr or proc.stdout, selected)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"available": True, "logged_in": False, "profile": selected,
                "error": "invalid JSON from bohr"}
    data = payload.get("data") or {}
    return {
        "available": True,
        "logged_in": bool(data.get("logged_in")),
        "profile": data.get("profile") or selected or "",
        "auth_method": data.get("auth_method") or "",
        "credential_source": data.get("credential_source") or "",
        "host": data.get("host") or "",
        "ak_present": bool(data.get("ak_present")),
        "error": "" if data.get("logged_in") else "not logged in",
    }


def bohr_version(runtime: Runtime) -> str:
    if not runtime.bohr_bin:
        return ""
    try:
        proc = run([runtime.bohr_bin, "version", "-o", "json"],
                   check=False, timeout=15)
    except Exception:  # noqa: BLE001
        return ""
    if proc.returncode != 0:
        return ""
    try:
        payload = json.loads(proc.stdout)
        return str((payload.get("data") or {}).get("version") or "")
    except json.JSONDecodeError:
        text = (proc.stdout or "").strip().splitlines()
        return text[0][:120] if text else ""


def python_version(python: str) -> str:
    try:
        proc = run([python, "-c", "import sys; print(sys.version.split()[0])"],
                   check=False, timeout=15)
    except Exception:  # noqa: BLE001
        return ""
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""
