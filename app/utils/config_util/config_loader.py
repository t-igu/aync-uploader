# app/config_loader.py
import os
from pathlib import Path
from .hot_config import HotConfig

# ---- CONFIG_PATH を環境変数から受け取る ----
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH") or "")

if not CONFIG_PATH.exists():
    raise RuntimeError(f"Config file not found: {CONFIG_PATH}")

# ---- HotConfig を初期化（アプリ全体で共有する唯一のインスタンス）----
config = HotConfig(path=CONFIG_PATH)

class ConfigSection:
    """HotConfig の特定セクションへのアクセスを容易にし、動的な更新を維持するプロキシクラス"""
    def __init__(self, section_name: str):
        self.section_name = section_name

    def get(self, key: str, default=None):
        # 常に最新の HotConfig インスタンスから値を取得する
        return config.get().get(self.section_name, {}).get(key, default)


app_config = ConfigSection("app")
api_config = ConfigSection("api")
worker_config = ConfigSection("worker")

# print("config", config.get())