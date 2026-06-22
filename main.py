import asyncio
import logging

from dotenv import load_dotenv
import uvicorn

from src.common.utils.log_util import ensure_logging_config
from src.app import app


def main():
    """启动 FastAPI 服务"""
    load_dotenv()
    ensure_logging_config()
    logging.getLogger(__name__).info("[main] 启动 FastAPI 服务 | host=0.0.0.0 | port=8000")
    uvicorn.run(app, host="0.0.0.0", port=32388)


if __name__ == "__main__":
    main()
