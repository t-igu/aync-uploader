import contextvars
import importlib
import logging
import multiprocessing as mp
import queue
import os
import sys
import time
import traceback
from typing import Any, Optional, Union

import msgspec
import structlog
from structlog.types import Processor, EventDict

# request_id を保持するための ContextVar
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# 独自のTRACEレベルを定義 (DEBUG: 10 より下の 5)
TRACE_LEVEL_NUM = 5

# structlog の内部マッピングに TRACE を登録し、KeyError と TypeError を回避する
try:
    # Pylance の PrivateImportUsage を回避するため動的にインポートして操作
    _sn = importlib.import_module("structlog._native")
    getattr(_sn, "LEVEL_TO_NAME")[TRACE_LEVEL_NUM] = "trace"
    getattr(_sn, "NAME_TO_LEVEL")["trace"] = TRACE_LEVEL_NUM
except (ImportError, AttributeError, TypeError):
    # structlog のバージョンにより構造が異なる場合のフォールバック
    pass

logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

class TraceLogWriter:
    """別プロセスで実行される書き込み専用クラス"""
    @staticmethod
    def run(log_queue_obj: mp.Queue, file_path: str, buffer_limit: int = 64 * 1024):
        # 高速化のため os.open を使用し、バイナリモードで追記
        fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        with os.fdopen(fd, "ab", buffering=0) as f:
            buffer = []
            buffer_bytes = 0
            while True:
                try:
                    item = log_queue_obj.get(timeout=1.0)
                    if item is None:  # 終了シグナル
                        if buffer:
                            f.write(b"".join(buffer))
                        break
                    
                    line = item + b"\n"
                    buffer.append(line)
                    buffer_bytes += len(line)

                    # 設定したバッファサイズを超えたら一括書き込み
                    if buffer_bytes >= buffer_limit:
                        f.write(b"".join(buffer))
                        buffer.clear()
                        buffer_bytes = 0
                except queue.Empty:
                    if buffer:
                        f.write(b"".join(buffer))
                        buffer.clear()
                        buffer_bytes = 0
                    continue
                except KeyboardInterrupt:
                    break
                except Exception:
                    continue

class TraceLog:
    def __init__(self, log_file: str, service_name: str = "app", queue_size: int = 100_000):
        self._service_name = service_name
        self._start_times: dict[str, float] = {}
        self._encoder: Optional[msgspec.json.Encoder] = None
        
        # 非同期書き込み用キュー
        self._ctx = mp.get_context("fork" if os.name != "nt" else "spawn")
        self._queue = self._ctx.Queue(maxsize=queue_size)
        
        # 書き込みプロセス開始
        self._writer = self._ctx.Process(
            target=TraceLogWriter.run,
            args=(self._queue, log_file),
            daemon=False
        )
        self._writer.start()

        # structlog の設定
        self._setup_structlog()
        self._logger = structlog.get_logger()
        
        # FastAPI / HTTPX (標準logging) の統合
        self._integrate_standard_logging()

    @property
    def encoder(self) -> msgspec.json.Encoder:
        """msgspec Encoderを遅延初期化（Pickle対策）"""
        if self._encoder is None:
            self._encoder = msgspec.json.Encoder()
        return self._encoder

    def __getstate__(self):
        """マルチプロセッシング（Windows/spawn）での pickle 送信時の処理"""
        state = self.__dict__.copy()
        # pickle 化できない、または不要なオブジェクトを除外
        state["_encoder"] = None
        state["_writer"] = None
        state["_logger"] = None
        return state

    def __setstate__(self, state):
        """子プロセスでの受信（unpickle）時の処理"""
        self.__dict__.update(state)
        # 子プロセス側でロギングの再設定
        self._setup_structlog()
        self._logger = structlog.get_logger()
        # 子プロセスでも標準ログの統合が必要なら実行
        self._integrate_standard_logging()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()

    _STR_TO_LEVEL = {
        "trace": TRACE_LEVEL_NUM,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }

    def _setup_structlog(self):
        """structlogのパイプライン設定"""
        processors: list[Processor] = [
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            self._queue_processor, # 自作プロセッサでQueueへ流す
        ]
        
        structlog.configure(
            processors=processors,
            logger_factory=structlog.PrintLoggerFactory(), # 内部で使わないので軽量なものを指定
            cache_logger_on_first_use=True,
        )

    def _queue_processor(self, logger: Any, method_name: str, event_dict: EventDict) -> Any:
        """structlog のイベントを JSON 化して Queue に入れる"""
        event_dict["service"] = self._service_name
        # msgspec が処理できない exc_info (tuple) を削除（format_exc_info で文字列化済み）
        if "exc_info" in event_dict:
            event_dict.pop("exc_info")
        try:
            # msgspec.to_builtins で非シリアライズ対象をクレンジング
            # これにより、exc_info 等が含まれていても安全に JSON 化できます
            clean_event = msgspec.to_builtins(event_dict)
            self._queue.put_nowait(self.encoder.encode(clean_event))
        except queue.Full:
            # キューが満杯の場合はドロップ（パフォーマンス優先）
            pass
        except Exception:
            pass
        
        # structlog 自体の後続処理（コンソール出力等）は不要なため DropPickle 的な挙動
        raise structlog.DropEvent

    def _integrate_standard_logging(self):
        """FastAPIやhttpxの標準ログをキャッチして structlog に流す"""
        class InterceptHandler(logging.Handler):
            def emit(self, record):
                # ログレベルの取得
                level = record.levelno
                kwargs = {
                    "event": record.getMessage(),
                    "logger_name": record.name,
                    "process_id": record.process,
                    "thread_name": record.threadName,
                }
                event = kwargs.pop("event")
                if record.exc_info:
                    kwargs["exc_info"] = record.exc_info

                structlog.get_logger("stdlib").log(level, event, **kwargs)

        # 全てのハンドラを削除して入れ替え
        root_logger = logging.getLogger()
        for h in root_logger.handlers[:]:
            root_logger.removeHandler(h)
        root_logger.addHandler(InterceptHandler())
        root_logger.setLevel(TRACE_LEVEL_NUM)

    @classmethod
    def _convert_data(cls, data: Any) -> Any:
        """msgspec, pydantic, dict を msgspec でシリアライズ可能な形式に変換"""
        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        # msgspec.Struct, Pydantic, dataclasses などを高速に dict/list へ変換
        try:
            return msgspec.to_builtins(data)
        except Exception:
            return str(data)


    def _log(self, level: str, data: Any, event_message: Optional[str] = None, **kwargs):
        """structlogを呼び出す共通メソッド (整数レベルを使用して TypeError を回避)"""
        request_id = request_id_ctx.get()
        level_lower = level.lower()
        # 文字列から整数レベルへ変換。未定義なら INFO(20)
        level_int = self._STR_TO_LEVEL.get(level_lower, logging.INFO)
        # event_message が指定されていない場合は request_id を event 名として使用
        self._logger.log(level_int, event_message or request_id, request_id=request_id, data=self._convert_data(data), **kwargs)

    def debug(self, data: Any = None, event_message: Optional[str] = None):
        self._log("debug", data, event_message=event_message)

    def error(self, data: Any = None, exc_info: Optional[Any] = None, event_message: Optional[str] = None, is_end: bool = False):
        """エラーログを出力。is_end=True の場合は計測も終了する"""
        if is_end:
            self.end(data=data, event_message=event_message, level="error", exc_info=exc_info)
        else:
            self._log("error", data, exc_info=exc_info, event_message=event_message)

    def info(self, data: Any = None, event_message: Optional[str] = None, is_end: bool = False):
        """infoログを出力。is_end=True の場合は計測も終了する"""
        if is_end:
            self.end(data=data, event_message=event_message, level="info")
        else:
            self._log("info", data, event_message=event_message)

    def trace(self, data: Any = None, event_message: Optional[str] = None):
        """カスタムレベル TRACE でのログ出力"""
        self._log("trace", data, event_message=event_message)

    def warning(self, data: Any = None, event_message: Optional[str] = None):
        self._log("warning", data, event_message=event_message)

    def start(self, request_id: str, data: Any = None, event_message: Optional[str] = None, level: str="info"):
        """計測開始ログ"""
        request_id_ctx.set(request_id)
        self._start_times[request_id] = time.perf_counter()
        self._log(level, data, event_message=event_message or "start")

    def end(self, data: Any = None, event_message: Optional[str] = None, level: Optional[str] = None, exc_info: Optional[Any] = None, **kwargs):
        """計測終了ログ。statusに応じたレベルの自動判別と例外情報の付与を行う"""
        request_id = request_id_ctx.get()
        start_time = self._start_times.pop(request_id, None)
        elapsed = (time.perf_counter() - start_time) * 1000.0 if start_time else None

        log_data = self._convert_data(data) or {}
        
        # 1. ステータスコードからレベルを自動判定 (レベル未指定時)
        status = log_data.get("status")
        if level is None:
            level = "error" if (isinstance(status, int) and status >= 500) or exc_info else "info"

        # 2. 例外情報 (exc_info) があれば、errorメッセージとtracebackを自動付与
        if exc_info:
            if "error" not in log_data:
                log_data["error"] = str(exc_info)
            if "traceback" not in log_data:
                if hasattr(exc_info, "__traceback__") and exc_info.__traceback__:
                    log_data["traceback"] = "".join(traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__))
                else:
                    # exc_infoがオブジェクトでなく、かつ現在のコンテキストに例外がある場合のフォールバック
                    log_data["traceback"] = traceback.format_exc()

        self._log(level, log_data, event_message=event_message or "end", elapsed_ms=elapsed, **kwargs)

    def shutdown(self):
        """ロガーの終了処理。未出力のログをフラッシュする"""
        # 親プロセス（_writer を保持しているプロセス）のみが終了処理を行う
        if self._queue and getattr(self, "_writer", None):
            try:
                self._queue.put(None, timeout=2)
                # Writerプロセスがバッファを書き終えるのを待つ
                max_wait = 5.0
                start_wait = time.time()
                while self._writer.is_alive():
                    if time.time() - start_wait > max_wait:
                        break
                    time.sleep(0.1)
            except Exception:
                self._writer.terminate()
