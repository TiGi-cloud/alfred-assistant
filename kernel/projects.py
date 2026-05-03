"""
Per-project context: cwd + env vars + default model.

Each user has a set of named projects and an "active" pointer. The
ClaudeRunner asks `ProjectRegistry.context_for(ctx)` which returns the
project's cwd + env (or sensible defaults). Switching project = switching
both your working directory and your environment for the next Claude call.

Stored in alfred_projects.json:

  {
    "telegram:u1": {
      "active": "myapp",
      "projects": {
        "myapp": {
          "cwd": "/Users/alice/Code/myapp",
          "env": {"DEBUG": "true"},
          "model": "claude-sonnet-4-6"
        }
      }
    }
  }
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .runner import Context

logger = logging.getLogger("alfred.kernel.projects")


@dataclass
class ProjectContext:
    name: str
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    model: Optional[str] = None


@dataclass
class ProjectRegistry:
    state_path: Optional[Path] = None
    _state: dict[str, dict] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.state_path is None:
            self.state_path = Path(__file__).resolve().parent.parent / "alfred_projects.json"
        self._load()

    # -- Persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text())
            except Exception:
                self._state = {}

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.write_text(json.dumps(self._state, indent=2))
        except Exception:
            logger.warning("Failed to persist projects registry")

    @staticmethod
    def _user_key(ctx: Context) -> str:
        return f"{ctx.adapter.name}:{ctx.user.id}"

    # -- CRUD --------------------------------------------------------------
    def list_projects(self, ctx: Context) -> dict[str, dict]:
        return dict(self._state.get(self._user_key(ctx), {}).get("projects", {}))

    def add(
        self,
        ctx: Context,
        name: str,
        *,
        cwd: str,
        env: Optional[dict[str, str]] = None,
        model: Optional[str] = None,
    ) -> dict:
        cwd_p = Path(cwd).expanduser().resolve()
        if not cwd_p.is_dir():
            raise ValueError(f"cwd does not exist or isn't a directory: {cwd_p}")
        info = {"cwd": str(cwd_p), "env": dict(env or {}), "model": model}
        self._state.setdefault(self._user_key(ctx), {}).setdefault("projects", {})[name] = info
        self._save()
        return info

    def remove(self, ctx: Context, name: str) -> bool:
        u = self._state.get(self._user_key(ctx), {})
        projects = u.get("projects", {})
        if name in projects:
            projects.pop(name)
            if u.get("active") == name:
                u.pop("active", None)
            self._save()
            return True
        return False

    def set_env(self, ctx: Context, name: str, key: str, value: Optional[str]) -> bool:
        u = self._state.get(self._user_key(ctx), {})
        info = u.get("projects", {}).get(name)
        if not info:
            return False
        env = info.setdefault("env", {})
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
        self._save()
        return True

    def set_model(self, ctx: Context, name: str, model: Optional[str]) -> bool:
        u = self._state.get(self._user_key(ctx), {})
        info = u.get("projects", {}).get(name)
        if not info:
            return False
        info["model"] = model
        self._save()
        return True

    # -- Active ------------------------------------------------------------
    def active(self, ctx: Context) -> Optional[str]:
        return self._state.get(self._user_key(ctx), {}).get("active")

    def set_active(self, ctx: Context, name: Optional[str]) -> bool:
        u = self._state.setdefault(self._user_key(ctx), {})
        if name is None:
            u.pop("active", None)
            self._save()
            return True
        if name not in u.get("projects", {}):
            return False
        u["active"] = name
        self._save()
        return True

    def context_for(self, ctx: Context) -> Optional[ProjectContext]:
        """Return the active ProjectContext for this user, or None."""
        u = self._state.get(self._user_key(ctx), {})
        name = u.get("active")
        if not name:
            return None
        info = u.get("projects", {}).get(name)
        if not info:
            return None
        return ProjectContext(
            name=name,
            cwd=Path(info["cwd"]),
            env=dict(info.get("env", {})),
            model=info.get("model"),
        )
