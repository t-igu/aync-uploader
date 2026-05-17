[Unit]
Description=Async File Uploader API

[Service]
Environment="CONFIG_PATH=/data/project/config/config.toml"
ExecStart=/usr/bin/python3 /data/project/app/main.py
Restart=always
User=www-data
WorkingDirectory=/data/project/app

[Install]
WantedBy=multi-user.target
