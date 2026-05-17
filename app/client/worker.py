import time
import traceback
from app.utils.config_util.config_loader import worker_config

def process_job(job, worker_id, logger, upload_queue):
    max_retry = worker_config.get("max_retry", 3)

    request_id = job["request_id"]   # header の request_id
    db_id = job["id"]                # DB の 18 桁 ID
    file_path = job["file_path"]     # ファイル単位
    username = job["username"]
    retry = job.get("retry", 0)

    # ---- start ----
    logger.start(
        request_id=request_id,
        data={
            "worker_id": worker_id,
            "retry": retry,
            "db_id": db_id,
            "file_path": file_path,
            "username": username,
        },
        event_message="worker_job_start"
    )

    try:
        # ---- 実際のアップロード処理 ----
        logger.info(
            event_message="worker_upload",
            data={"worker_id": worker_id, "file_path": file_path}
        )

        # upload_to_salesforce(file_path)

        print(f"UPLOAD SUCCESS request_id={request_id} file={file_path}")

        # ---- success ----
        logger.end(
            data={
                "worker_id": worker_id,
                "status": "success",
                "db_id": db_id,
                "file_path": file_path,
            },
            event_message="worker_job_end"
        )

    except Exception as e:
        # ---- retry 1〜3回目：warning ----
        if retry < max_retry:
            logger.warning(
                event_message="worker_upload_failed",
                data={
                    "worker_id": worker_id,
                    "request_id": request_id,
                    "db_id": db_id,
                    "file_path": file_path,
                    "retry": retry,
                    "error": str(e),
                }
            )

            # retry queue に再投入
            job["retry"] = retry + 1
            upload_queue.put(job)

            logger.end(
                data={
                    "worker_id": worker_id,
                    "status": "retry",
                    "retry": retry + 1,
                    "db_id": db_id,
                    "file_path": file_path,
                },
                event_message="worker_job_end"
            )
            return

        # ---- 4回目の失敗：error + DLQ ----
        logger.error(
            event_message="worker_upload_failed_final",
            data={
                "worker_id": worker_id,
                "request_id": request_id,
                "db_id": db_id,
                "file_path": file_path,
                "retry": retry,
                "error": str(e),
            },
            is_end=True
        )

        from dead_letter import write_dead_letter
        write_dead_letter(request_id, job, "upload_failed", logger)
        return


def main(upload_queue, worker_id: int, logger):
    logger.info(event_message="worker_start", data={"worker_id": worker_id})

    while True:
        job = upload_queue.get()

        if job is None:
            logger.info(event_message="worker_sentinel", data={"worker_id": worker_id})
            break

        logger.info(
            event_message="worker_received",
            data={
                "worker_id": worker_id,
                "request_id": job["request_id"],
                "db_id": job["id"],
                "file_path": job["file_path"],
            }
        )

        process_job(job, worker_id, logger, upload_queue)

    logger.info(event_message="worker_exit", data={"worker_id": worker_id})
