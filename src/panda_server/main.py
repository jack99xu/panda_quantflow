from common.logging.system_logger import logging, setup_logging
import mimetypes
import sys
import uvicorn
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from panda_server.config.database import mongodb
from panda_server.config.env import *
from panda_plugins.utils.work_node_loader import load_all_nodes
from panda_server.messaging.consumer_manager import QueueConsumerManager
from panda_server.messaging.rabbitmq_client import AsyncRabbitMQ
from panda_server.routes import (
    base_routes,
    plugins_routes,
    workflow_routes,
    backtest_route,
    chat_routes,
)
from starlette.staticfiles import StaticFiles
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse

from panda_server.routes.trading import (
    trading_routes,
    trading_report_routes
)

# Add project root path to python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))  

# Load .env file if exsits
import dotenv
dotenv.load_dotenv()

logger = logging.getLogger(__name__)

# 获取当前文件所在目录的父目录的父目录（即项目根目录）
BASE_DIR = Path(__file__).resolve().parent.parent.parent
print("base_dir:" + str(BASE_DIR))

# 将项目根目录下的 src 目录加入模块搜索路径
sys.path.append(str(BASE_DIR / "src"))

# Define lifespan for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle management"""

    # MongoDB connection logic
    logger.info("Connecting to MongoDB...")
    await mongodb.connect_db()
    logger.info("MongoDB connection successful")
    
    # Initialize local database indexes
    await mongodb.init_local_db()

    logger.info("Loading work nodes...")
    load_all_nodes()
    logger.info("Work nodes loading completed")

    # RabbitMQ connection logic
    # CLOUD模式使用RabbitMQ队列，LOCAL模式直接操作数据库
    if RUN_MODE == "CLOUD":
        # Test RabbitMQ connection
        rabbitmq_client = AsyncRabbitMQ()
        logger.info(f"CLOUD mode: Connecting to RabbitMQ...")
        await rabbitmq_client.test_connect()
        logger.info("RabbitMQ connection successful")

        # start queue consumers only when server_role is CONSUMER or ALL
        if SERVER_ROLE in ["CONSUMER","ALL"]:
            logger.info("RabbitMQ CONSUMER start")
            consumer_manager = QueueConsumerManager()
            await consumer_manager.start_all_consumers(rabbitmq_client)
    else:
        logger.info(f"LOCAL mode: RabbitMQ not required, will use direct database operations")
        rabbitmq_client = None

    # Application runtime
    yield

    # Shutdown logic
    logger.info("Closing MongoDB connection...")
    await mongodb.close_db()
    logger.info("MongoDB connection closed")
    # CLOUD模式需要关闭RabbitMQ连接
    if RUN_MODE == "CLOUD" and rabbitmq_client is not None:
        logger.info("Closing RabbitMQ connection...")
        await rabbitmq_client.close()
        logger.info("RabbitMQ connection closed")


app = FastAPI(
    title="PandaAI QuantFlow API",
    description="PandaAI QuantFlow API",
    version="1.0.0",
    lifespan=lifespan,
)

# 获取当前文件所在目录
current_dir = Path(__file__).resolve().parent
# 获取上一级目录
parent_dir = current_dir.parent
# 获取上一级目录中的另一个文件夹
frontend_folder = parent_dir / "panda_web"
logger.info(f"前端静态资源文件夹路径:{frontend_folder}")
# 显式设置 .js .css 的 MIME 类型
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")
app.mount("/quantflow", StaticFiles(directory=frontend_folder, html=True), name="quantflow")
app.mount("/charts", StaticFiles(directory=frontend_folder, html=True), name="charts")

# SPA 回退：/quantflow/* 和 /charts/* 下找不到资源的路径返回 index.html
@app.get("/quantflow/{full_path:path}")
@app.get("/charts/{full_path:path}")
async def quantflow_spa(full_path: str):
    index_file = frontend_folder / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse(status_code=404)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Should set specific domains in production environment
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(base_routes.router)
app.include_router(plugins_routes.router)
app.include_router(workflow_routes.router)
app.include_router(backtest_route.router)
app.include_router(chat_routes.router)

app.include_router(trading_routes.router)

app.include_router(trading_report_routes.router)

if __name__ == "__main__":
    # Deliberately repeated once to prevent other project modules from overriding logging configuration
    setup_logging()
    # Start Uvicorn server
    uvicorn.run(
        "main:app",
        reload=True,
        host="0.0.0.0",
        port=8000,
        log_config=None,  # Disable Uvicorn default log configuration
    )
