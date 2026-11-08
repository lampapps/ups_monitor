"""Serve the web UI with gunicorn (production) or Flask's dev server."""

from gunicorn.app.base import BaseApplication

from .. import settings
from ..config import load_config
from . import create_app


class _Gunicorn(BaseApplication):
    def __init__(self, app, options: dict):
        self._app = app
        self._options = options
        super().__init__()

    def load_config(self) -> None:
        for key, value in self._options.items():
            self.cfg.set(key, value)

    def load(self):
        return self._app


def serve(dev: bool = False) -> None:
    settings.load_env()
    cfg = load_config(check_env=False)
    app = create_app(cfg=cfg)
    if dev:
        app.run(host=cfg.web.host, port=cfg.web.port, debug=False)
        return
    _Gunicorn(
        app,
        {
            "bind": f"{cfg.web.host}:{cfg.web.port}",
            "workers": 2,
            "threads": 2,
            "worker_class": "gthread",
            "timeout": 30,
            "errorlog": "-",
        },
    ).run()
