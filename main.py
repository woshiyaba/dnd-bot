import argparse
import logging

from dotenv import load_dotenv
import uvicorn

from src.common.debug import configure_debug
from src.common.utils.log_util import ensure_logging_config


def _parse_args():
    """解析服务启动参数。"""
    parser = argparse.ArgumentParser(description="启动 DND BOT FastAPI 服务")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="收集 LangGraph 节点输入、输出和系统提示词并推送到前端",
    )
    return parser.parse_args()


def main():
    """启动 FastAPI 服务"""
    args = _parse_args()
    load_dotenv()
    ensure_logging_config()
    configure_debug(args.debug)
    from src.app import app

    logging.getLogger(__name__).info(
        "[main] 启动 FastAPI 服务 | host=0.0.0.0 | port=32388 | debug=%s",
        args.debug,
    )
    uvicorn.run(app, host="0.0.0.0", port=32388)


if __name__ == "__main__":
    main()
