"""User-managed overlay for prompts and systemd units.

The dashboard lets the user edit prompts and add/edit/remove timers +
services without going through git. Their edits land in an **overlay**
directory outside the git tree so they:

  - survive ``git reset --hard`` from the auto-deploy (the deploy never
    touches anything under ``/var/lib/orclaw/``),
  - never appear as ``git status`` noise,
  - default cleanly back to the shipped versions when the user clears
    the override (a "revert" is just deleting the overlay file).

Layout under ``OverlayPaths.root`` (production: ``/var/lib/orclaw/overrides``)::

    prompts/   # *.md — wins over the git-tracked /opt/orclaw/prompts/
    systemd/   # *.timer + *.service — wins over /opt/orclaw/infra/systemd/

Resolution rules
----------------

When the engine asks for a prompt or a unit, we check the overlay first
and fall back to the default tree if no override exists. Each accessor
also returns an ``is_overridden`` flag so the UI can badge them.

Systemd side effects
--------------------

Writing a unit via :func:`set_timer` doesn't just touch the overlay —
it also publishes the new content to ``/etc/systemd/system/<name>`` (so
systemd actually sees it), runs ``daemon-reload``, and ``try-restart``s
the unit. Deleting a user-created unit stops + disables + unlinks. The
operations shell out via ``sudo``; the dashboard runs as the ``engine``
user which has ``NOPASSWD: ALL`` for exactly this reason.

This module is the single seam between "user clicked save" and "the
running system reflects it" — it stays small + functional so tests can
drive the overlay without ever touching systemd by passing a custom
:class:`OverlayPaths`.
"""

from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from orclaw.logging import get_logger

log = get_logger(__name__)

_DEFAULT_PROD_ROOT = Path("/var/lib/orclaw/overrides")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PROMPTS = _REPO_ROOT / "prompts"
_DEFAULT_SYSTEMD = _REPO_ROOT / "infra" / "systemd"
_SYSTEMD_CANONICAL = Path("/etc/systemd/system")


# --- Data --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverlayPaths:
    """Where the overlay + the defaults live.

    Tests pass a custom instance with all dirs under ``tmp_path`` so they
    can drive the overlay without ever touching ``/var/lib`` or systemd.
    """

    root: Path
    """Override root — typically ``/var/lib/orclaw/overrides``."""

    defaults_prompts: Path = _DEFAULT_PROMPTS
    defaults_systemd: Path = _DEFAULT_SYSTEMD
    systemd_canonical: Path = _SYSTEMD_CANONICAL
    """Where systemd actually reads units. Edits get published here too."""

    apply_to_systemd: bool = True
    """When False, :func:`set_timer` / :func:`delete_timer` skip the
    daemon-reload/try-restart side effects. Tests set this to False so
    they can verify overlay file ops in isolation."""

    @property
    def prompts(self) -> Path:
        return self.root / "prompts"

    @property
    def systemd(self) -> Path:
        return self.root / "systemd"


@dataclass(frozen=True, slots=True)
class PromptInfo:
    name: str
    size_bytes: int
    is_overridden: bool


@dataclass(frozen=True, slots=True)
class TimerInfo:
    name: str
    kind: Literal["timer", "service"]
    size_bytes: int
    is_overridden: bool
    is_user_created: bool
    """True when there is no default version — the file only exists in
    the overlay. Deleting these fully removes the unit; deleting an
    overridden default just reverts."""


def default_paths() -> OverlayPaths:
    """Production overlay paths. Subdirs created on demand when writable.

    Read paths gracefully when the overlay root doesn't exist or isn't
    writable (e.g. running on a dev machine where ``/var/lib/orclaw``
    isn't provisioned) — the overlay simply has nothing to offer and we
    fall through to the shipped defaults.
    """
    paths = OverlayPaths(root=_DEFAULT_PROD_ROOT)
    # Best-effort mkdir: read accessors tolerate a missing dir (they fall
    # back to the shipped defaults), and writers — the dashboard — will
    # raise loudly when they can't create the overlay.
    for sub in (paths.prompts, paths.systemd):
        with contextlib.suppress(PermissionError, OSError):
            sub.mkdir(parents=True, exist_ok=True)
    return paths


# --- Prompts ----------------------------------------------------------------


def list_prompts(paths: OverlayPaths) -> list[PromptInfo]:
    """Union of shipped prompts + user-created overlay prompts."""
    seen: dict[str, PromptInfo] = {}
    if paths.defaults_prompts.is_dir():
        for f in sorted(paths.defaults_prompts.glob("*.md")):
            seen[f.name] = PromptInfo(name=f.name, size_bytes=f.stat().st_size, is_overridden=False)
    if paths.prompts.is_dir():
        for f in sorted(paths.prompts.glob("*.md")):
            seen[f.name] = PromptInfo(name=f.name, size_bytes=f.stat().st_size, is_overridden=True)
    return sorted(seen.values(), key=lambda p: p.name)


def resolve_prompt_path(paths: OverlayPaths, name: str) -> Path:
    """Return the path the engine should read for ``name`` — overlay wins."""
    overlay = paths.prompts / name
    if overlay.is_file():
        return overlay
    return paths.defaults_prompts / name


def read_prompt(paths: OverlayPaths, name: str) -> tuple[str, bool]:
    """Read the effective prompt. Returns ``(content, is_overridden)``.

    Raises ``FileNotFoundError`` if neither overlay nor default exists.
    """
    overlay = paths.prompts / name
    if overlay.is_file():
        return overlay.read_text(encoding="utf-8"), True
    default = paths.defaults_prompts / name
    if default.is_file():
        return default.read_text(encoding="utf-8"), False
    raise FileNotFoundError(name)


def set_prompt(paths: OverlayPaths, name: str, content: str) -> None:
    """Persist a prompt override. No git side effects."""
    paths.prompts.mkdir(parents=True, exist_ok=True)
    (paths.prompts / name).write_text(content, encoding="utf-8")
    log.info("overlay_prompt_set", name=name, bytes=len(content))


def delete_prompt_override(paths: OverlayPaths, name: str) -> bool:
    """Revert a prompt to the shipped default (delete the overlay file).

    Returns True if an overlay existed and was removed, False if there
    was nothing to revert. We *never* delete the default file itself —
    that would require a git change.
    """
    overlay = paths.prompts / name
    if not overlay.exists():
        return False
    overlay.unlink()
    log.info("overlay_prompt_reverted", name=name)
    return True


# --- Timers / services ------------------------------------------------------


def _ensure_unit_name(name: str) -> Literal["timer", "service"]:
    if name.endswith(".timer"):
        return "timer"
    if name.endswith(".service"):
        return "service"
    raise ValueError(f"unit name must end in .timer or .service: {name!r}")


def list_timers(paths: OverlayPaths) -> list[TimerInfo]:
    """Union of shipped units + user-created overlay units."""
    defaults: dict[str, Path] = {}
    if paths.defaults_systemd.is_dir():
        for f in paths.defaults_systemd.iterdir():
            if f.suffix in (".timer", ".service"):
                defaults[f.name] = f

    overrides: dict[str, Path] = {}
    if paths.systemd.is_dir():
        for f in paths.systemd.iterdir():
            if f.suffix in (".timer", ".service"):
                overrides[f.name] = f

    all_names = sorted(defaults.keys() | overrides.keys())
    out: list[TimerInfo] = []
    for name in all_names:
        path = overrides.get(name) or defaults[name]
        out.append(
            TimerInfo(
                name=name,
                kind=_ensure_unit_name(name),
                size_bytes=path.stat().st_size,
                is_overridden=name in overrides,
                is_user_created=name in overrides and name not in defaults,
            )
        )
    # timers first within each name (more useful in UI), then services
    return sorted(out, key=lambda t: (t.kind != "timer", t.name))


def read_timer(paths: OverlayPaths, name: str) -> tuple[str, TimerInfo]:
    """Read the effective unit content + metadata. Raises FileNotFoundError."""
    _ensure_unit_name(name)
    overlay = paths.systemd / name
    default = paths.defaults_systemd / name
    has_default = default.is_file()
    if overlay.is_file():
        content = overlay.read_text(encoding="utf-8")
        size = overlay.stat().st_size
        return content, TimerInfo(
            name=name,
            kind=_ensure_unit_name(name),
            size_bytes=size,
            is_overridden=True,
            is_user_created=not has_default,
        )
    if has_default:
        content = default.read_text(encoding="utf-8")
        return content, TimerInfo(
            name=name,
            kind=_ensure_unit_name(name),
            size_bytes=default.stat().st_size,
            is_overridden=False,
            is_user_created=False,
        )
    raise FileNotFoundError(name)


def set_timer(
    paths: OverlayPaths,
    name: str,
    content: str,
    *,
    enable_if_new: bool = True,
) -> TimerInfo:
    """Write a unit override + publish to systemd.

    Steps when ``paths.apply_to_systemd`` is True (the default):
      1. Write the overlay file.
      2. ``sudo cp`` it to ``/etc/systemd/system/<name>`` so systemd
         sees it (the deploy script also writes there from the repo;
         our copy clobbers the deploy version for as long as the
         overlay exists).
      3. ``sudo systemctl daemon-reload``.
      4. If brand new + ``enable_if_new`` + it's a ``.timer`` → enable
         and start it. ``.service`` units only ever fire from a timer
         (or are oneshots), so we don't auto-enable those.
      5. Otherwise ``sudo systemctl try-restart <name>`` so existing
         instances pick up the new content on next run.
    """
    _ensure_unit_name(name)
    paths.systemd.mkdir(parents=True, exist_ok=True)
    overlay_path = paths.systemd / name
    is_brand_new = not overlay_path.exists() and not (paths.defaults_systemd / name).exists()
    overlay_path.write_text(content, encoding="utf-8")

    if paths.apply_to_systemd:
        _publish_to_systemd(overlay_path, paths.systemd_canonical / name)
        _systemctl("daemon-reload")
        if is_brand_new and enable_if_new and name.endswith(".timer"):
            _systemctl("enable", "--now", name)
        else:
            _systemctl("try-restart", name)

    _, info = read_timer(paths, name)
    log.info(
        "overlay_timer_set",
        name=name,
        brand_new=is_brand_new,
        bytes=len(content),
    )
    return info


def delete_timer(paths: OverlayPaths, name: str) -> Literal["reverted", "removed"]:
    """Remove a unit override.

    - If a default exists in the git tree → "reverted": the overlay is
      removed, the default is restored to ``/etc/systemd/system/``,
      daemon-reload + try-restart.
    - If the unit was user-created (no default) → "removed": fully
      stops + disables it, unlinks from systemd, deletes overlay.
    """
    _ensure_unit_name(name)
    overlay_path = paths.systemd / name
    default_path = paths.defaults_systemd / name
    has_overlay = overlay_path.is_file()
    has_default = default_path.is_file()

    if not has_overlay and not has_default:
        raise FileNotFoundError(name)

    if has_default:
        # Revert: drop overlay, restore default content to /etc, reload.
        if has_overlay:
            overlay_path.unlink()
        if paths.apply_to_systemd:
            _publish_to_systemd(default_path, paths.systemd_canonical / name)
            _systemctl("daemon-reload")
            _systemctl("try-restart", name)
        log.info("overlay_timer_reverted", name=name)
        return "reverted"

    # User-created: full removal.
    if paths.apply_to_systemd:
        _systemctl_quiet("stop", name)
        _systemctl_quiet("disable", name)
        _sudo_rm(paths.systemd_canonical / name)
        _systemctl("daemon-reload")
    overlay_path.unlink(missing_ok=True)
    log.info("overlay_timer_removed", name=name)
    return "removed"


def reapply_systemd_overrides(paths: OverlayPaths) -> list[str]:
    """Re-publish every overlay unit to ``/etc/systemd/system/``.

    Hook for the auto-deploy script: after the deploy copies fresh
    ``infra/systemd/*`` to ``/etc/systemd/system/``, we run this so the
    user's overlays win again. Returns the list of unit names refreshed.
    """
    if not paths.systemd.is_dir():
        return []
    refreshed: list[str] = []
    for f in paths.systemd.iterdir():
        if f.suffix in (".timer", ".service"):
            if paths.apply_to_systemd:
                _publish_to_systemd(f, paths.systemd_canonical / f.name)
            refreshed.append(f.name)
    if refreshed and paths.apply_to_systemd:
        _systemctl("daemon-reload")
    log.info("overlay_systemd_reapplied", count=len(refreshed))
    return refreshed


# --- sudo helpers -----------------------------------------------------------


def _publish_to_systemd(src: Path, dst: Path) -> None:
    """``sudo cp src dst`` — needs root for ``/etc/systemd/system/``.

    Tests can bypass by passing ``apply_to_systemd=False``; this helper
    is only ever called inside that guard.
    """
    # cp -f to overwrite even if the deploy script just put a different
    # version there. The destination is owned by root, mode 644.
    subprocess.run(
        ["sudo", "-n", "cp", str(src), str(dst)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["sudo", "-n", "chmod", "644", str(dst)],
        check=True,
        capture_output=True,
    )


def _systemctl(*args: str) -> None:
    """Strict systemctl call — raises CalledProcessError on failure."""
    subprocess.run(
        ["sudo", "-n", "systemctl", *args],
        check=True,
        capture_output=True,
    )


def _systemctl_quiet(*args: str) -> None:
    """Best-effort systemctl call. Used for stop/disable where the unit
    may already be inactive — we don't want a benign "Unit not loaded"
    to surface as an error.
    """
    proc = subprocess.run(
        ["sudo", "-n", "systemctl", *args],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        log.info(
            "overlay_systemctl_quiet_nonzero",
            args=args,
            stderr=proc.stderr.decode(errors="replace")[:200],
        )


def _sudo_rm(path: Path) -> None:
    subprocess.run(
        ["sudo", "-n", "rm", "-f", str(path)],
        check=True,
        capture_output=True,
    )
