import atexit
import os
import re
import subprocess
from pathlib import Path

from app import app
from config import DEBUG, VITE_BASE_API, DASHBOARD_PATH
from fastapi.staticfiles import StaticFiles

base_dir = Path(__file__).parent
build_dir = base_dir / 'build'
statics_dir = build_dir / 'statics'

# Content-hashed asset filenames (e.g. index.2edadd5e.js) never change
# content, so they're safe to cache forever. Everything else — index.html,
# 404.html, locales/*.json — must revalidate; otherwise a browser keeps a
# stale index.html that points at old chunk hashes, which 404 after a deploy
# and crash the SPA (also handled client-side via vite:preloadError reload).
_HASHED_ASSET_RE = re.compile(r"\.[0-9a-f]{8,}\.[A-Za-z0-9]+$")


class CachedStaticFiles(StaticFiles):
    """StaticFiles with version-aware Cache-Control: `immutable` for
    content-hashed assets, `no-cache` (always revalidate) for HTML and
    locale JSON so a new deploy is picked up immediately."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        name = os.path.basename(str(getattr(response, "path", "")))
        if _HASHED_ASSET_RE.search(name):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def build():
    proc = subprocess.Popen(
        ['npm', 'run', 'build', '--',  '--outDir', build_dir, '--assetsDir', 'statics'],
        env={**os.environ, 'VITE_BASE_API': VITE_BASE_API},
        cwd=base_dir
    )
    proc.wait()
    with open(build_dir / 'index.html', 'r') as file:
        html = file.read()
    with open(build_dir / '404.html', 'w') as file:
        file.write(html)


def run_dev():
    proc = subprocess.Popen(
        ['npm', 'run', 'dev', '--', '--host', '0.0.0.0', '--clearScreen', 'false', '--base', os.path.join(DASHBOARD_PATH, '')],
        env={**os.environ, 'VITE_BASE_API': VITE_BASE_API},
        cwd=base_dir
    )

    atexit.register(proc.terminate)


def run_build():
    if not build_dir.is_dir():
        build()

    app.mount(
        DASHBOARD_PATH,
        CachedStaticFiles(directory=build_dir, html=True),
        name="dashboard"
    )
    app.mount(
        '/statics/',
        CachedStaticFiles(directory=statics_dir, html=True),
        name="statics"
    )


@app.on_event("startup")
def startup():
    if DEBUG:
        run_dev()
    else:
        run_build()
