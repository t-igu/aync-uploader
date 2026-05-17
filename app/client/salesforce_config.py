# config/salesforce_config.py

SF_BASE_URL = "https://your-instance.my.salesforce.com"
SF_LOGIN_URL = "https://login.salesforce.com"
SF_CLIENT_ID = "xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SF_USERNAME = "your-user@example.com"
SF_JWT_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----"""

SF_TOKEN_LEEWAY = 30  # 秒: expire の少し前に更新
SF_HTTP_TIMEOUT = 30
SF_HTTP_RETRY_COUNT = 3
SF_HTTP_RETRY_DELAY = 1
