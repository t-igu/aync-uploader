import time
import signal
import sys
import uvicorn
from multiprocessing import Process, Queue, cpu_count
from .utils.log_utils.trace_log import TraceLog
from .api import api_server
from .client import worker
from .utils.config_util.config_loader import app_config, api_config

upload_queue: Queue
logger: TraceLog
running: bool = True
processes: list[Process] = []


def start_api(upload_queue: Queue, logger: TraceLog):
    api_server.set_queue(upload_queue)

    app = api_server.create_app(logger)
    app.state.queue = upload_queue

    uvicorn.run(
        app,
        host=api_config.get("host", "0.0.0.0"),
        port=api_config.get("port", 8800),
        log_config=None,
    )


def start_worker(upload_queue: Queue, worker_id: int, logger: TraceLog):
    worker.main(upload_queue, worker_id, logger)


def supervisor():
    global upload_queue, logger, running, processes

    logger = TraceLog(
        app_config.get("log_file", "app.log"),
        service_name=app_config.get("service_name", "async-file-uploader"),
    )

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

        # ★★ 人間向けログ（print） ★★
        print(f"SUPERVISOR STOPPED signal={signum}")
        logger.info(event_message="supervisor_exit")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    upload_queue = Queue()
    worker_count = cpu_count() * 2

    p_api = Process(target=start_api, args=(upload_queue, logger), name="api")
    processes.append(p_api)
    p_api.start()
    logger.info(event_message="api_started", data={"pid": p_api.pid})

    for i in range(worker_count):
        p = Process(target=start_worker, args=(upload_queue, i, logger), name=f"worker-{i}")
        processes.append(p)
        p.start()
        logger.info(event_message="worker_started", data={"pid": p.pid, "worker_id": i})

    while running:
        time.sleep(1)
        for p in processes:
            if not p.is_alive():
                logger.error(
                    event_message="process_died",
                    data={"name": p.name, "exitcode": p.exitcode},
                )
                running = False
                break


if __name__ == "__main__":
    supervisor()
