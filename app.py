"""服务入口：python app.py（或 uvicorn app:app）"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from douyin.session import BrowserPool
from spark import __version__
from spark.clock import Clock
from spark.config import GlobalConfig, load_env
from spark.logger import setup_logging
from spark.scheduler import SparkScheduler
from spark.store import Store
from web.api import AppContext, build_router

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "web" / "static"


def create_app() -> FastAPI:
    load_env(BASE_DIR / ".env")
    gcfg = GlobalConfig.from_env()
    setup_logging(gcfg.data_dir, gcfg.log_level)

    store = Store(gcfg.db_path())
    clock = Clock(gcfg.tz)
    pool = BrowserPool(gcfg.accounts_dir(), headless=gcfg.headless,
                       max_contexts=gcfg.max_contexts, tz=gcfg.tz, proxy=gcfg.proxy)
    sched = SparkScheduler(gcfg, store, clock, pool)
    ctx = AppContext(gcfg=gcfg, store=store, clock=clock, pool=pool, sched=sched)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        import asyncio

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, clock.sync, gcfg.ntp_servers)
        sched.start()
        yield
        await sched.stop()

    app = FastAPI(title="火花管家 Pro", version=__version__, lifespan=lifespan)
    app.include_router(build_router(ctx))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8020")), log_level="warning")
