FROM python:3.13-slim

# 安装 MongoDB
RUN apt-get update && apt-get install -y gnupg curl git build-essential openssl && \
    curl -fsSL https://www.mongodb.org/static/pgp/server-8.0.asc | gpg -o /usr/share/keyrings/mongodb-server-8.0.gpg --dearmor && \
    echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-8.0.gpg ] https://repo.mongodb.org/apt/debian bookworm/mongodb-org/8.0 main" | tee /etc/apt/sources.list.d/mongodb-org-8.0.list && \
    apt-get update && apt-get install -y mongodb-org && \
    rm -rf /var/lib/apt/lists/*

# 创建虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" VIRTUAL_ENV="/opt/venv"

# 预装所有 Python 依赖（排除需源码编辑安装的 panda_factor/panda_quantflow）
RUN pip install --no-cache-dir \
    numpy pandas fastapi uvicorn python-multipart \
    pymongo redis pymysql DBUtils motor \
    loguru PyYAML python-json-logger \
    xgboost scikit-learn tqdm \
    python-dateutil pytz pydantic \
    tensorflow lightgbm openpyxl \
    tushare ratelimit statsmodels \
    python-snappy matplotlib tornado \
    rqdatac cloudpickle torch \
    aio-pika jsonpickle async-timeout \
    json2html uuid6 python-dotenv \
    python-socketio openai ipython \
    requests PyJWT pytest websockets \
    brotli zstandard msgpack orjson \
    python-rapidjson click flask \
    aiohttp beautifulsoup4 lxml

# 时区
ENV TZ=Asia/Shanghai
