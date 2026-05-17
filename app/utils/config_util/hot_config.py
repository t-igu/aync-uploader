# app/hot_config.py
from __future__ import annotations

import logging
import os
import platform
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

import tomllib
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, DirModifiedEvent
from watchdog.observers import Observer

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

log = logging.getLogger(__name__)


class ConfigLoadError(Exception):
    pass


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigLoadError(f"Config file not found: {path}")

    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        raise ConfigLoadError(f"Failed to load TOML: {e}") from e


class HotConfig:
    def __init__(
        self,
        path: Path,
        *,
        on_update: Optional[Callable[[dict[str, Any]], None]] = None,
        debounce_sec: float = 0.1,
    ) -> None:

        self._path = path.resolve()
        self._on_update = on_update
        self._debounce_sec = debounce_sec

        self._lock = threading.RLock()
        self._config: Optional[dict[str, Any]] = None

        self._observer: BaseObserver | None = None
        self._stop_event = threading.Event()
        self._last_event_ts = 0.0

        self._register_signal_handlers()

        # 初回ロード（失敗したらアプリ起動を止める）
        self._reload(initial=True)

        # ファイル監視開始
        self._start_watcher()

    # -----------------------------
    # Public API
    # -----------------------------
    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._config is None:
                raise RuntimeError("Config not loaded")
            return self._config

    def stop(self) -> None:
        self._stop_event.set()

        if self._observer is not None and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5)

        log.info("HotConfig watcher stopped")

    # -----------------------------
    # Internal
    # -----------------------------
    def _reload(self, *, initial: bool = False) -> None:
        try:
            new_conf = _load_toml(self._path)

            with self._lock:
                self._config = new_conf

            if not initial:
                log.info("Config reloaded from %s", self._path)

            if self._on_update:
                try:
                    self._on_update(new_conf)
                except Exception:
                    log.exception("on_update callback failed")

        except Exception as e:
            if initial:
                log.error("Initial config load failed: %s", e)
                raise
            else:
                log.error("Config reload failed, keeping previous config: %s", e)

    def _start_watcher(self) -> None:
        class Handler(FileSystemEventHandler):
            def __init__(self, outer: HotConfig) -> None:
                self._outer = outer

            def on_modified(self, event) -> None:
                if self._outer._stop_event.is_set():
                    return

                # FileModifiedEvent / DirModifiedEvent の両方を許可
                if not isinstance(event, (FileModifiedEvent, DirModifiedEvent)):
                    return

                event_path = Path(os.fsdecode(event.src_path)).resolve()
                if event_path != self._outer._path:
                    return

                now = time.time()
                if now - self._outer._last_event_ts < self._outer._debounce_sec:
                    return
                self._outer._last_event_ts = now

                log.info("Config file modified: %s", event_path)
                self._outer._reload(initial=False)

        observer = Observer()
        handler = Handler(self)

        observer.schedule(handler, self._path.parent.as_posix(), recursive=False)
        observer.daemon = True
        observer.start()

        self._observer = observer
        log.info("Started config watcher on %s", self._path)

    def _register_signal_handlers(self):
        # Windows は signal 再送が危険なので無効化
        if platform.system() == "Windows":
            return

        def handler(signum, frame):
            log.info(f"Received signal {signum}, stopping HotConfig...")
            try:
                self.stop()
            except Exception:
                log.exception("Failed to stop HotConfig cleanly")

            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, handler)
