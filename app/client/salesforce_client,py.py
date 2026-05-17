import asyncio
import httpx
from pathlib import Path
import time
from typing import Optional

# 方式を「Upload Sessions」に変更:
# SalesforceのConnect API（/connect/files/users/me/binary-upload-sessions）を使用するように変更しました。
# これはバイナリファイルを分割して PUT し、最後に Commit を送ることで、メモリ消費を抑えつつ最大2GBまでのファイルを確実にアップロードできる、Salesforce推奨の方法です。
# _ensure_token のバグ修正:
# トークンを新規取得した後に return self._access_token が漏れていたため、正しくトークンが返るように修正しました。
# request メソッドの堅牢化:
# resp 変数の初期化漏れによる UnboundLocalError を防ぐ対策を行いました。
# 401エラー（トークン切れ）が発生した際、ループ内で確実に新しいトークンを再取得してリトライするようにロジックを整理しました。
# リソース管理:
# upload_file_chunked では、ファイルを開きながら CHUNK_SIZE（5MB）ずつ読み込んで送信するため、数GBのファイルを扱ってもサーバーのRAMを圧迫しません。

from .salesforce_config import (
    SF_BASE_URL,
    SF_TOKEN_LEEWAY,
    SF_HTTP_TIMEOUT,
    SF_HTTP_RETRY_COUNT,
    SF_HTTP_RETRY_DELAY,
)
from app.utils.log_utils.trace_log import TraceLog
from .salesforce_jwt import fetch_salesforce_token

class SalesforceClient:
    API_VERSION = "v61.0"

    def __init__(self, logger: TraceLog):
        self.logger = logger
        self._access_token: Optional[str] = None
        self._expire_at: int = 0
        self._lock = asyncio.Lock()
        self.CHUNK_SIZE = 5 * 1024 * 1024  # 5MB
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=SF_HTTP_TIMEOUT)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=SF_HTTP_TIMEOUT)
        return self._client

    async def _ensure_token(self) -> str:
        async with self._lock:
            now = int(time.time())
            if self._access_token is not None and now <= (self._expire_at - SF_TOKEN_LEEWAY):
                return self._access_token

            self.logger.info(
                event_message="sf_token_refresh_start",
                data={"now": now, "expire_at": self._expire_at},
            )
            self._access_token, self._expire_at = await fetch_salesforce_token(self.logger)
            self.logger.info(
                event_message="sf_token_refresh_done",
                data={"expire_at": self._expire_at},
            )
            return self._access_token

    async def _create_upload_session(self, filename: str):
        """Connect APIを使用してアップロードセッションを開始する"""
        path = f"/services/data/{self.API_VERSION}/connect/files/users/me/binary-upload-sessions"
        resp = await self.request("POST", path, json={"fileName": filename})
        resp.raise_for_status()
        return resp.json()["id"]  # uploadSessionId

    async def _upload_part(self, session_id: str, part_number: int, chunk: bytes):
        """セッションに対してパーツ（チャンク）をアップロードする"""
        path = f"/services/data/{self.API_VERSION}/connect/files/users/me/binary-upload-sessions/{session_id}/parts/{part_number}"
        headers = {"Content-Type": "application/octet-stream"}
        resp = await self.request("PUT", path, content=chunk, headers=headers)
        resp.raise_for_status()

    async def _commit_upload(self, session_id: str):
        """アップロードを完了させ、ファイルを確定する"""
        path = f"/services/data/{self.API_VERSION}/connect/files/users/me/binary-upload-sessions/{session_id}"
        # state=Commit で確定。これにより ContentDocument/ContentVersion が生成される
        resp = await self.request("POST", path, json={"state": "Commit"})
        resp.raise_for_status()
        return resp.json()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{SF_BASE_URL}{path}"
        client = self._get_client()
        resp = None

        try:
            headers = kwargs.pop("headers", {}) or {}
            headers.setdefault("Content-Type", "application/json")
            
            for attempt in range(2):  # 401時のリトライを含めて最大2回試行
                token = await self._ensure_token()
                headers["Authorization"] = f"Bearer {token}"
                
                self.logger.info(event_message="sf_request", data={"method": method, "url": url, "attempt": attempt})
                resp = await http_request_with_retry(client, method, url, headers=headers, **kwargs)
                
                if resp.status_code != 401:
                    break
                
                self.logger.warning(event_message="sf_unauthorized", data={"url": url})
                self._access_token = None

            if resp is None:
                raise RuntimeError(f"Request failed to return a response: {method} {url}")

        except Exception as e:
            self.logger.error(event_message="sf_request_exception", data={"url": url, "error": str(e)})
            raise

        if resp.is_error:
            self.logger.error(
                event_message="sf_response_error",
                data={
                    "url": url,
                    "status": resp.status_code,
                    "body": resp.text[:500]
                },
            )
        else:
            self.logger.info(
                event_message="sf_response_ok",
                data={"url": url, "status": resp.status_code},
            )

        return resp

    async def upload_file_chunked(self, filepath: str, **extra):
        """
        本番環境で最大2GBまでのファイルを安全に送信する分割アップロード
        """
        filename = Path(filepath).name
        session_id = await self._create_upload_session(filename)
        self.logger.info(event_message="sf_upload_session_started", data={"session_id": session_id})

        part_number = 1
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(self.CHUNK_SIZE)
                if not chunk:
                    break

                await self._upload_part(session_id, part_number, chunk)
                self.logger.info(
                    event_message="sf_chunk_uploaded",
                    data={"session_id": session_id, "part": part_number},
                )
                part_number += 1

        result = await self._commit_upload(session_id)
        self.logger.info(event_message="sf_upload_complete", data={"file": filename, "result": result})
        return result

async def http_request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """低レイヤのHTTPリトライロジックのみを担当"""
    kwargs.pop("request_id", None)
    kwargs.pop("download", None)
    kwargs.pop("timeout", None)

    for attempt in range(SF_HTTP_RETRY_COUNT):
        try:
            resp = await client.request(method, url, **kwargs)
            return resp
        except httpx.RequestError:
            if attempt == SF_HTTP_RETRY_COUNT - 1:
                raise
            await asyncio.sleep(SF_HTTP_RETRY_DELAY)

    raise RuntimeError("unreachable")  # 型チェック対策
