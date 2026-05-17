# run.py
import time
import signal
import sys
from multiprocessing import Process, Queue, cpu_count
from app.utils.log_utils.trace_log import TraceLog
import uvicorn
from app.api import api_server
from app.client import worker

# ---- グローバル変数（型だけ宣言、初期化はしない） ----
upload_queue: Queue
logger: TraceLog
running: bool = True
processes: list[Process] = []


def start_api(upload_queue: Queue, logger: TraceLog):
    api_server.set_queue(upload_queue)

    app = api_server.create_app(logger)
    app.state.queue = upload_queue

    # log_config=None を指定して TraceLog の InterceptHandler を維持する
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


def start_worker(upload_queue: Queue, worker_id: int, logger: TraceLog):
    worker.main(upload_queue, worker_id, logger)


def supervisor():
    global upload_queue, logger, running, processes

    # 全プロセスで共有する唯一の TraceLog インスタンス
    logger = TraceLog("app.log", service_name="async-file-uploader")

    def handle_signal(signum, frame):
        global running
        logger.info(event_message="supervisor_signal", data={"signal": signum})
        running = False

        # Worker に sentinel を送る
        for _ in processes:
            upload_queue.put(None)

        # 子プロセス終了
        for p in processes:
            if p.is_alive():
                logger.info(event_message="supervisor_terminate", data={"pid": p.pid, "name": p.name})
                p.terminate()

        logger.info(event_message="supervisor_exit")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    upload_queue = Queue()

    worker_count = cpu_count() * 2

    # API プロセス
    p_api = Process(target=start_api, args=(upload_queue, logger), name="api")
    processes.append(p_api)
    p_api.start()
    logger.info(event_message="api_started", data={"pid": p_api.pid})

    # 監視ループ
    while True:
        time.sleep(1)
        for p in processes:
            if not p.is_alive():
                logger.error(
                    event_message="process_died",
                    data={"name": p.name, "exitcode": p.exitcode}
                )
                return

if __name__ == "__main__":
    supervisor()
