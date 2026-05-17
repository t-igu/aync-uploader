# models.py
import msgspec
from typing import Annotated

# ---- msgspec モデル（body はリスト） ----
class UploadItem(msgspec.Struct):
    id: Annotated[str, msgspec.Meta(min_length=18, max_length=18)]
    filename: Annotated[str, msgspec.Meta(min_length=1)]
    filename_disp: str
    encrypted_filepath: str
    extension: str
    username: str

class DownloadRequest(msgspec.Struct):
    file_paths: list[UploadItem]


class UploadJob(msgspec.Struct):
    request_id: str
    file_paths: list[str]


