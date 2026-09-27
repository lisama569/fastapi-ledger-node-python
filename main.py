import uvicorn

from node.api import app  # noqa: F401  re-export for `uvicorn main:app`

__all__ = ["app"]


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
