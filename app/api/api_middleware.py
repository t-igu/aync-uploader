import sys
import traceback
import uuid
from litestar.middleware import ASGIMiddleware
from litestar.enums import ScopeType
from litestar import Request
from litestar.types import Scope, Receive, Send, ASGIApp

class TraceLogMiddleware(ASGIMiddleware):
    scopes = (ScopeType.HTTP,)

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        request = Request(scope, receive=receive)
        # app.state から TraceLog インスタンスを取得
        logger = getattr(scope["app"].state, "logger", None)

        # X-Request-ID があれば使用、なければ新規生成
        request_id = request.headers.get("X-Request-ID") or "x-"+str(uuid.uuid4())

        if logger:
            # TraceLog の start メソッドを呼び出し（ContextVar に request_id がセットされます）
            logger.start(
                request_id=request_id,
                data={
                    "path": request.url.path,
                    "method": request.method,
                    "query": request.query_params.dict(),
                    "client": request.client.host if request.client else None,
                },
                event_message="api_request_start"
            )

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status = message["status"]
                if logger:
                    # TraceLog側で例外の抽出とレベル判定を自動で行う
                    logger.end(
                        data={"status": status},
                        event_message="api_request_end",
                        exc_info=scope.get("exception") or sys.exc_info()[1]
                    )
            await send(message)

        try:
            await next_app(scope, receive, send_wrapper)

        except Exception as exc:
            if logger:
                # 予期せぬ例外時も is_end=True で1行に集約
                logger.error(
                    data={"status": "failed"},
                    event_message="api_request_end",
                    exc_info=exc,
                    is_end=True
                )
            raise
