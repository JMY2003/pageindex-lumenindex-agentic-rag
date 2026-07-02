from pathlib import Path

from pageindex_web.logging_config import configure_logging
from pageindex_web.main import create_app

configure_logging(Path(__file__).resolve().parent)
app = create_app()

if __name__ == "__main__":
    import os
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    uvicorn.run("run_web:app", host=host, port=port, reload=False)
