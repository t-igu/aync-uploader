import time
import jwt  # PyJWT

from .salesforce_config import (
    SF_CLIENT_ID,
    SF_USERNAME,
    SF_LOGIN_URL,
    SF_JWT_PRIVATE_KEY,
)

def create_salesforce_jwt() -> str:
    now = int(time.time())
    payload = {
        "iss": SF_CLIENT_ID,
        "sub": SF_USERNAME,
        "aud": SF_LOGIN_URL,
        "exp": now + 60 * 3,  # 3分くらい
    }
    token = jwt.encode(payload, SF_JWT_PRIVATE_KEY, algorithm="RS256")
    return token

import httpx
from .salesforce_config import SF_LOGIN_URL

from app.utils.log_utils.trace_log import TraceLog

async def fetch_salesforce_token(logger: "TraceLog") -> tuple[str, int]:
    jwt_assertion = create_salesforce_jwt()

    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_assertion,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{SF_LOGIN_URL}/services/oauth2/token", data=data)

    if resp.status_code != 200:
        logger.error(
            event_message="sf_token_error",
            data={"status": resp.status_code, "body": resp.text},
        )
        resp.raise_for_status()

    body = resp.json()
    access_token = body["access_token"]
    instance_url = body.get("instance_url")  # 必要なら使う

    # expires_in は秒数
    expires_in = int(body.get("expires_in", 60 * 10))
    expire_at = int(time.time()) + expires_in

    logger.info(
        event_message="sf_token_issued",
        data={"expire_at": expire_at, "expires_in": expires_in},
    )

    return access_token, expire_at
