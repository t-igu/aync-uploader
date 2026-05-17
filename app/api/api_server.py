from pathlib import Path
from typing import Any, Dict, List
from litestar import Litestar, post, get, Request, Response
from .api_middleware import TraceLogMiddleware
from litestar.exceptions import HTTPException
from litestar.params import Parameter
import msgspec
from app.utils.config_util.config_loader import api_config, app_config
from app.models.models import DownloadRequest, UploadItem

# ---- グローバル（spawn-safe） ----
upload_queue = None

def set_queue(q):
    global upload_queue
    upload_queue = q

@get("/check")
async def health_check() -> dict:
    return {"status": "ok"}

UPLOAD_ROOT = Path(app_config.get("data_root")).resolve()

def directory_traversal_check(target: Path) -> bool:
    try:
        resolved = target.resolve()
        return UPLOAD_ROOT == resolved or UPLOAD_ROOT in resolved.parents
    except Exception:
        return False


@post("/upload")
async def upload(
    request: Request,
    request_id: str = Parameter(header="X-Request-ID"),
) -> Response:

    if upload_queue is None:
        raise HTTPException(status_code=500, detail="Queue not initialized")

    # ---- body を型なし decode（dict として読む）----
    raw = await request.body()
    try:
        raw_dict: Dict[str, Any] = msgspec.json.decode(raw, type=dict, strict=False)
    except msgspec.DecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # ---- "request" が存在するかチェック ----
    if "request" not in raw_dict or not isinstance(raw_dict["request"], list):
        raise HTTPException(status_code=400, detail="Invalid body: 'request' must be a list")

    raw_items: List[Any] = raw_dict["request"]
    items: List[UploadItem] = []
    errors: List[Dict[str, Any]] = []

    # ---- 個別に UploadItem に変換（型チェック）----
    for idx, raw_item in enumerate(raw_items):
        try:
            item = msgspec.convert(raw_item, UploadItem)
            items.append(item)
        except Exception as e:
            errors.append({
                "index": idx,
                "raw": raw_item,
                "error": f"parameter validation error: {e}"
            })

    # ---- traversal チェック ----
    for idx, item in enumerate(items):
        target = Path(item.filename)
        if not directory_traversal_check(target):
            errors.append({
                "index": idx,
                "id": item.id,
                "file_path": item.filename,
                "error": "directory traversal detected"
            })

    # ---- エラーがあれば返す ----
    if errors:
        raise HTTPException(status_code=400, detail=errors)  # type: ignore[arg-type]

    # ---- Queue の上限チェック ----
    qsize = upload_queue.qsize()
    queue_limit = api_config.get("queue_limit", 10_000)
    if qsize >= queue_limit:
        return Response(
            content={"detail": "Too many requests. Please retry later."},
            status_code=429
        )

    # ---- 正常な item だけ Queue に投入 ----
    for item in items:
        job = {
            "request_id": request_id,
            "id": item.id,
            "file_path": item.filename,
            "username": item.username,
            "retry": 0,
        }
        upload_queue.put(job)

    return Response(
        content={"status": "accepted", "request_id": request_id, "count": len(items)},
        status_code=202
    )


def create_app(logger=None):
    app = Litestar(
        route_handlers=[health_check, upload],
        middleware=[TraceLogMiddleware()],
        debug=True,
    )
    if logger:
        app.state.logger = logger
    return app
