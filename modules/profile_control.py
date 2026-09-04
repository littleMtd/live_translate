"""Hot-reload control plane for source profile selection and profile data."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Callable

from config import cfg
from modules.activity_context import activity_publication_store, normalize_activity
from modules.profile_context import (
    ProfileState,
    profile_resolution_status,
    profile_state,
)
from utils.logger import get_logger
from utils.runtime_events import runtime_events

log = get_logger("profile_control")

_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _ROOT / "logs" / "live_translate_config.json"
REGISTRY_PATH = _ROOT / "data" / "streamer_profiles.json"
STATUS_PATH = _ROOT / "logs" / "profile_status.json"


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _atomic_status_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


class ProfileControlWatcher:
    def __init__(
        self,
        *,
        state: ProfileState = profile_state,
        config_path: Path = CONFIG_PATH,
        registry_path: Path = REGISTRY_PATH,
        status_path: Path = STATUS_PATH,
        interval: float = 1.0,
        clock: Callable[[], float] = time.time,
    ):
        self._state = state
        self._config_path = config_path
        self._registry_path = registry_path
        self._status_path = status_path
        self._interval = max(0.1, interval)
        self._clock = clock
        self._config_stamp = _stamp(config_path)
        self._registry_stamp = _stamp(registry_path)
        self._last_status_payload: dict[str, object] | None = None

    def publish_status(self) -> None:
        payload = self._state.current().as_metadata()
        payload.update(profile_resolution_status.current())
        manual_activity = normalize_activity(getattr(cfg.translation, "current_activity", ""))
        automatic_activity = activity_publication_store.current()
        payload["activity"] = (
            manual_activity
            or (automatic_activity.display_label if automatic_activity is not None else "")
        )
        payload["activity_source"] = (
            "manual" if manual_activity else "automatic" if automatic_activity else "none"
        )
        if payload == self._last_status_payload:
            return
        self._last_status_payload = dict(payload)
        payload["updated_at"] = self._clock()
        _atomic_status_write(self._status_path, payload)

    def _reload_config(self) -> None:
        data = json.loads(self._config_path.read_text(encoding="utf-8"))
        translation = data.get("translation")
        stt = data.get("stt", {})
        if not isinstance(translation, dict) or not isinstance(stt, dict):
            raise ValueError("dashboard config sections are invalid")
        source = translation.get("streamer_profile")
        mode = translation.get("profile_mode", "auto")
        use_profile = translation.get("use_profile")
        use_glossary = stt.get("use_profile_glossary")
        if not isinstance(source, str):
            raise ValueError("streamer_profile must be a string")
        if mode not in {"auto", "manual"}:
            raise ValueError("profile_mode must be auto or manual")
        if not isinstance(use_profile, bool) or not isinstance(use_glossary, bool):
            raise ValueError("profile application flags must be booleans")
        current = self._state.current()
        canonical = self._state.registry.canonical_id(source)
        if canonical is None:
            raise ValueError(f"unknown streamer_profile: {source!r}")
        desired = (canonical, mode, use_profile, use_glossary)
        existing = (
            current.source_profile_id,
            current.mode,
            current.translation_profile_applied,
            current.stt_glossary_applied,
        )
        if desired != existing:
            snapshot = self._state.configure_source(
                source,
                mode=mode,
                translation_profile_applied=use_profile,
                stt_glossary_applied=use_glossary,
            )
            runtime_events.emit("profile_control_reload", status="applied", **snapshot.as_metadata())

    def poll_once(self) -> None:
        registry_stamp = _stamp(self._registry_path)
        if registry_stamp != self._registry_stamp:
            try:
                snapshot = self._state.reload_registry(self._registry_path)
                self._registry_stamp = registry_stamp
                runtime_events.emit("profile_registry_reload", status="applied", **snapshot.as_metadata())
            except Exception as exc:
                self._registry_stamp = registry_stamp
                log.warning("Profile registry reload rejected; retaining prior generation: %s", exc)
                runtime_events.emit("profile_registry_reload", status="rejected", error_type=type(exc).__name__)

        config_stamp = _stamp(self._config_path)
        if config_stamp != self._config_stamp:
            try:
                self._reload_config()
                self._config_stamp = config_stamp
            except Exception as exc:
                self._config_stamp = config_stamp
                log.warning("Profile config reload rejected; retaining prior generation: %s", exc)
                runtime_events.emit("profile_control_reload", status="rejected", error_type=type(exc).__name__)
        self.publish_status()

    def run(self, stop_event: threading.Event) -> None:
        self.publish_status()
        while not stop_event.wait(self._interval):
            self.poll_once()


def start(stop_event: threading.Event) -> threading.Thread:
    watcher = ProfileControlWatcher()
    thread = threading.Thread(
        target=watcher.run,
        args=(stop_event,),
        name="ProfileControl",
        daemon=True,
    )
    thread.start()
    return thread
