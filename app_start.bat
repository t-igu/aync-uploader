@echo off

call .venv/Scripts/activate

set CONFIG_PATH=C:/workspace/python/github_projects/async_file_uploader/config/config.toml
set PYTHONPATH=%~dp0
python -m app.run
