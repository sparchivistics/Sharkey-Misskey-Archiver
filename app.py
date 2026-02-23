"""
Sharkey/Misskey Post Archiver
Archive posts from any public user on any Sharkey/Misskey instance.
No API key required.
"""

import re
import io
import json
import zipfile
import sqlite3
import hashlib
import mimetypes
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DB_PATH      = "archive_data/archive.db"
MEDIA_DIR    = Path("archive_data/media")
_server_port = 5757   # set at startup, used by take_screenshot


# ─── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id            TEXT PRIMARY KEY,
                instance      TEXT NOT NULL,
                note_id       TEXT NOT NULL,
                url           TEXT NOT NULL,
                archived_at   TEXT NOT NULL,
                user_name     TEXT,
                user_handle   TEXT,
                user_avatar   TEXT,
                content       TEXT,
                cw            TEXT,
                created_at    TEXT,
                reply_count   INTEGER DEFAULT 0,
                renote_count  INTEGER DEFAULT 0,
                reaction_count INTEGER DEFAULT 0,
                visibility    TEXT,
                raw_json      TEXT,
                screenshot_path TEXT
            );
            -- Add column if upgrading from older schema
            CREATE TABLE IF NOT EXISTS _migrations (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS media (
                id           TEXT PRIMARY KEY,
                post_id      TEXT NOT NULL,
                filename     TEXT NOT NULL,
                url          TEXT NOT NULL,
                mime_type    TEXT,
                local_path   TEXT,
                width        INTEGER,
                height       INTEGER,
                is_sensitive INTEGER DEFAULT 0,
                alt_text     TEXT,
                FOREIGN KEY (post_id) REFERENCES posts(id)
            );
        """)
        # Migrate older DBs that don't have screenshot_path yet
        try:
            db.execute("ALTER TABLE posts ADD COLUMN screenshot_path TEXT")
        except Exception:
            pass  # column already exists


# ─── Input Parsing ─────────────────────────────────────────────────────────────

def parse_input(raw: str):
    """
    Accept any of:
      - Full post URL:      https://instance.tld/notes/abc123
      - User profile URL:   https://instance.tld/@username
      - @user@instance.tld  (Fediverse handle)
      - username  +  instance field filled in separately

    Returns one of:
      ("note", instance, note_id)
      ("user", instance, username)
    """
    raw = raw.strip()
    parsed = urllib.parse.urlparse(raw)

    if parsed.scheme in ("http", "https") and parsed.netloc:
        instance = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path.rstrip("/")

        # /notes/<id>
        m = re.search(r"/notes/([A-Za-z0-9]+)$", path)
        if m:
            return ("note", instance, m.group(1))

        # Mastodon-compat /@user/posts/<id>  or  /@user/statuses/<id>
        m = re.search(r"/(?:posts|statuses)/([A-Za-z0-9]+)$", path)
        if m:
            return ("note", instance, m.group(1))

        # Profile URL  /@username
        m = re.match(r"^/@([A-Za-z0-9_.-]+)$", path)
        if m:
            return ("user", instance, m.group(1))

        raise ValueError(f"Cannot extract note or user from URL: {raw}")

    # @user@instance.tld  (Fediverse handle)
    m = re.match(r"^@?([A-Za-z0-9_.-]+)@([A-Za-z0-9_.-]+\.[A-Za-z]{2,})$", raw)
    if m:
        username, host = m.group(1), m.group(2)
        return ("user", f"https://{host}", username)

    # Bare username (instance provided separately)
    if re.match(r"^[A-Za-z0-9_.-]+$", raw):
        return ("user", None, raw)

    raise ValueError(f"Unrecognised input: {raw}")


# ─── Misskey / Sharkey API (no auth) ──────────────────────────────────────────

def api_post(instance: str, endpoint: str, payload: dict,
             retries: int = 3, base_delay: float = 2.0) -> dict:
    import time
    url  = f"{instance.rstrip('/')}/api/{endpoint}"
    data = json.dumps(payload).encode()

    last_err = None
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(base_delay * attempt)   # 2s, 4s back-off

        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "SharkeyArchiver/1.0"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # 500 = server overloaded / statement timeout — retry
            if e.code == 500 and attempt < retries - 1:
                last_err = f"API 500 (attempt {attempt+1}/{retries}), retrying…"
                print(f"  {last_err}")
                continue
            raise ValueError(f"API error {e.code} from {instance}: {body[:300]}")
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Request error ({e}), retrying…")
                last_err = str(e)
                continue
            raise

    raise ValueError(f"API request failed after {retries} attempts: {last_err}")


def lookup_user(instance: str, username: str) -> dict:
    return api_post(instance, "users/show", {"username": username})


def fetch_note(instance: str, note_id: str) -> dict:
    return api_post(instance, "notes/show", {"noteId": note_id})


def fetch_user_notes(instance: str, user_id: str,
                     limit: int = 20, until_id: str = None) -> list:
    # Keep batches small (max 20) to avoid server-side statement timeouts
    # on busy instances like eepy.moe
    payload = {
        "userId":         user_id,
        "limit":          min(limit, 20),
        "includeReplies": False,
        "withRenotes":    False,
    }
    if until_id:
        payload["untilId"] = until_id
    return api_post(instance, "users/notes", payload)


# ─── Media Download ────────────────────────────────────────────────────────────

def download_media(url: str, folder: str, file_id: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SharkeyArchiver/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ct  = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0]
            ext = mimetypes.guess_extension(ct) or ".bin"
            ext = {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)
            dest = MEDIA_DIR / folder / f"{file_id}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
            return str(dest)
    except Exception as e:
        print(f"  media fail: {url} — {e}")
        return None


# ─── Screenshot via Playwright ───────────────────────────────────────────────

# Temporary HTML store for the /render/ endpoint — keyed by a short token
_render_cache: dict = {}

def take_screenshot(post_id: str, html_content: str) -> str | None:
    """
    Render the HTML mirror in headless Chromium via the local HTTP server
    (avoids file:// path issues on Windows) and save a PNG screenshot.
    Returns the local path, or None if Playwright is not installed.

    Install once with:
        pip install playwright
        python -m playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None   # Playwright not installed — silently skip

    folder = re.sub(r"[^A-Za-z0-9_-]", "_", post_id)
    dest   = MEDIA_DIR / folder / "screenshot.png"
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Register the HTML under a token served by /render/<token>
    token = hashlib.md5(post_id.encode()).hexdigest()[:16]
    _render_cache[token] = html_content

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page    = browser.new_page(viewport={"width": 700, "height": 900})

            # Hit our own running HTTP server — no file:// path issues
            url = f"http://127.0.0.1:{_server_port}/render/{token}"
            page.goto(url, wait_until="domcontentloaded", timeout=15000)

            # Give images a moment to load
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass

            # Crop to just the post card
            card = page.query_selector(".card")
            if card:
                card.screenshot(path=str(dest))
            else:
                page.screenshot(path=str(dest), full_page=False)

            browser.close()
        return str(dest)
    except Exception as e:
        print(f"  screenshot failed for {post_id}: {e}")
        return None
    finally:
        _render_cache.pop(token, None)


# ─── Store a single note ───────────────────────────────────────────────────────

def store_note(instance: str, note: dict) -> str | None:
    note_id = note.get("id")
    if not note_id:
        return None

    post_id = f"{urllib.parse.urlparse(instance).netloc}/{note_id}"

    with get_db() as db:
        if db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
            return None  # already archived

        user         = note.get("user", {})
        user_name    = user.get("name") or user.get("username", "")
        handle_parts = [user.get("username", "")]
        if user.get("host"):
            handle_parts.append(user["host"])
        user_handle  = "@" + "@".join(handle_parts)
        user_avatar  = user.get("avatarUrl", "")

        reactions      = note.get("reactions", {})
        reaction_count = sum(reactions.values()) if isinstance(reactions, dict) else 0
        post_url       = f"{instance.rstrip('/')}/notes/{note_id}"
        now_iso        = datetime.utcnow().isoformat() + "Z"

        db.execute("""
            INSERT INTO posts
                (id, instance, note_id, url, archived_at, user_name, user_handle,
                 user_avatar, content, cw, created_at, reply_count, renote_count,
                 reaction_count, visibility, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            post_id, instance, note_id, post_url, now_iso,
            user_name, user_handle, user_avatar,
            note.get("text") or "",
            note.get("cw"),
            note.get("createdAt", ""),
            note.get("repliesCount", 0),
            note.get("renoteCount", 0),
            reaction_count,
            note.get("visibility", "public"),
            json.dumps(note),
        ))

        folder = re.sub(r"[^A-Za-z0-9_-]", "_", post_id)
        for f in note.get("files", []):
            fid   = f.get("id") or hashlib.md5(f.get("url", "").encode()).hexdigest()[:10]
            furl  = f.get("url", "")
            lpath = download_media(furl, folder, fid) if furl else None
            db.execute("""
                INSERT OR IGNORE INTO media
                    (id, post_id, filename, url, mime_type, local_path,
                     width, height, is_sensitive, alt_text)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                f"{post_id}/{fid}", post_id,
                f.get("name", fid), furl, f.get("type", ""),
                lpath,
                f.get("properties", {}).get("width"),
                f.get("properties", {}).get("height"),
                1 if f.get("isSensitive") else 0,
                f.get("comment", ""),
            ))

    # Generate screenshot of the HTML mirror (requires Playwright)
    html_for_screenshot = generate_html_mirror_for_screenshot(post_id)
    if html_for_screenshot:
        shot_path = take_screenshot(post_id, html_for_screenshot)
        if shot_path:
            with get_db() as db2:
                db2.execute("UPDATE posts SET screenshot_path=? WHERE id=?",
                            (shot_path, post_id))

    return post_id


# ─── Archive a single post ────────────────────────────────────────────────────

def archive_single(instance: str, note_id: str) -> dict:
    note    = fetch_note(instance, note_id)
    pid     = store_note(instance, note)
    netloc  = urllib.parse.urlparse(instance).netloc
    post_id = f"{netloc}/{note_id}"
    if pid is None:
        return {"status": "already_archived", "post_id": post_id}
    return {"status": "archived", "post_id": pid,
            "url": f"{instance.rstrip('/')}/notes/{note_id}"}


# ─── Archive all posts for a user ─────────────────────────────────────────────

def archive_user(instance: str, username: str,
                 max_posts: int = 500, progress_cb=None) -> dict:
    user     = lookup_user(instance, username)
    user_id  = user["id"]
    archived = 0
    skipped  = 0
    fetched  = 0
    until_id = None

    import time

    PAGE_SIZE  = 20    # small pages to avoid DB statement timeouts
    PAGE_DELAY = 1.0   # seconds between pages — be polite to the instance

    while fetched < max_posts:
        try:
            batch = fetch_user_notes(instance, user_id,
                                     limit=min(PAGE_SIZE, max_posts - fetched),
                                     until_id=until_id)
        except ValueError as e:
            err = str(e)
            # Surface a friendlier message for statement timeouts
            if "INTERNAL_ERROR" in err or "500" in err:
                raise ValueError(
                    f"The instance returned a server error while fetching posts. "
                    f"This usually means the server is under load. "
                    f"Try again in a few minutes, or reduce Max Posts. ({err[:120]})"
                )
            raise

        if not batch:
            break

        for note in batch:
            if store_note(instance, note):
                archived += 1
            else:
                skipped  += 1

        fetched  += len(batch)
        until_id  = batch[-1]["id"]
        if progress_cb:
            progress_cb(archived + skipped, fetched)

        if len(batch) < PAGE_SIZE:
            break   # last page

        time.sleep(PAGE_DELAY)   # pause before next page

    return {
        "status":   "done",
        "user":     username,
        "instance": instance,
        "archived": archived,
        "skipped":  skipped,
        "total":    fetched,
    }


def generate_html_mirror_for_screenshot(post_id: str) -> str | None:
    """Build the HTML mirror for Playwright to screenshot.
    We use embedded=False so media is served via /media/ HTTP routes —
    this avoids base64 data URI issues and is faster to render."""
    with get_db() as db:
        post  = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            return None
        media = db.execute("SELECT * FROM media WHERE post_id=?", (post_id,)).fetchall()
    return generate_html_mirror(dict(post), [dict(m) for m in media], embedded=False)


# ─── ZIP Export ───────────────────────────────────────────────────────────────

def create_zip_for_post(post_id: str) -> bytes | None:
    with get_db() as db:
        _post  = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not _post:
            return None
        post  = dict(_post)
        media = [dict(m) for m in db.execute("SELECT * FROM media WHERE post_id=?", (post_id,)).fetchall()]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        meta = dict(post)
        meta["raw_json"] = json.loads(meta.get("raw_json") or "{}")
        zf.writestr("post.json", json.dumps(meta, indent=2, ensure_ascii=False))
        zf.writestr("post.html", generate_html_mirror(post, media, embedded=True))
        for m in media:
            lp = m["local_path"]
            if lp and Path(lp).exists():
                zf.write(lp, f"media/{Path(lp).name}")
        # Include screenshot if available
        sp = post.get("screenshot_path")
        if sp and Path(sp).exists():
            zf.write(sp, "screenshot.png")
    return buf.getvalue()


# ─── HTML Mirror ──────────────────────────────────────────────────────────────

def generate_html_mirror(post, media, embedded=False):
    # Ensure post and media items are plain dicts so .get() works everywhere
    if not isinstance(post, dict):
        post = dict(post)
    media = [dict(m) if not isinstance(m, dict) else m for m in (media or [])]

    media_html = ""
    for m in media:
        mime = m["mime_type"] or ""
        if embedded and m["local_path"] and Path(m["local_path"]).exists():
            import base64
            raw = Path(m["local_path"]).read_bytes()
            src = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        else:
            src = m["url"]
        alt  = m["alt_text"] or m["filename"] or ""
        scls = " sensitive" if m["is_sensitive"] else ""
        if mime.startswith("image/"):
            media_html += f'<figure class="media-item{scls}"><img src="{src}" alt="{alt}" loading="lazy"><figcaption>{alt}</figcaption></figure>\n'
        elif mime.startswith("video/"):
            media_html += f'<figure class="media-item{scls}"><video src="{src}" controls></video><figcaption>{alt}</figcaption></figure>\n'
        elif mime.startswith("audio/"):
            media_html += f'<figure class="media-item{scls}"><audio src="{src}" controls></audio><figcaption>{alt}</figcaption></figure>\n'

    cw      = post["cw"]
    content = (post["content"] or "").replace("\n", "<br>")
    cw_html = (f'<div class="cw-warning" onclick="document.getElementById(\'pc\').classList.toggle(\'hidden\')">'
               f'<strong>⚠ Content Warning:</strong> {cw} <em>(click to reveal)</em></div>') if cw else ""
    hidden  = "hidden" if cw else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archived post by {post['user_handle']}</title>
<style>
  :root{{--bg:#0f1117;--card:#1a1d2e;--border:#2d3154;--accent:#7c6ff7;--text:#e2e4ef;--muted:#8b8fa8;--cw:#f59e0b}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:system-ui,sans-serif;padding:2rem 1rem}}
  .card{{max-width:640px;margin:0 auto;background:var(--card);border:1px solid var(--border);border-radius:16px;overflow:hidden}}
  .card-header{{padding:1.25rem 1.5rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:1rem}}
  .avatar{{width:48px;height:48px;border-radius:50%;background:var(--border);object-fit:cover}}
  .name{{font-weight:700}}.handle{{color:var(--muted);font-size:.85rem}}
  .card-body{{padding:1.5rem}}
  .cw-warning{{background:rgba(245,158,11,.15);border:1px solid var(--cw);color:var(--cw);padding:.75rem 1rem;border-radius:8px;margin-bottom:1rem;cursor:pointer}}
  .content{{line-height:1.7}}.hidden{{display:none}}
  .media-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:.75rem;margin-top:1.25rem}}
  .media-item{{border-radius:10px;overflow:hidden;background:#000}}
  .media-item img,.media-item video{{width:100%;display:block;max-height:480px;object-fit:cover}}
  .media-item.sensitive img,.media-item.sensitive video{{filter:blur(20px);cursor:pointer;transition:filter .3s}}
  .media-item.sensitive:hover img,.media-item.sensitive:hover video{{filter:none}}
  figcaption{{padding:.4rem .6rem;font-size:.75rem;color:var(--muted)}}
  .card-footer{{padding:1rem 1.5rem;border-top:1px solid var(--border);font-size:.85rem;color:var(--muted);display:flex;flex-wrap:wrap;gap:.75rem;align-items:center}}
  .badge{{background:var(--border);padding:.2rem .6rem;border-radius:99px;font-size:.75rem}}
  a{{color:var(--accent)}}
</style>
</head>
<body>
<div class="card">
  <div class="card-header">
    {'<img class="avatar" src="'+post["user_avatar"]+'" alt="">' if post["user_avatar"] else '<div class="avatar"></div>'}
    <div><div class="name">{post["user_name"] or post["user_handle"]}</div><div class="handle">{post["user_handle"]}</div></div>
    <div style="margin-left:auto"><span class="badge" style="background:rgba(124,111,247,.15);color:#a78bfa;border:1px solid #7c6ff7">Archived</span></div>
  </div>
  <div class="card-body">
    {cw_html}
    <div class="content {hidden}" id="pc">{content}</div>
    {('<div class="media-grid">'+media_html+'</div>') if media_html else ''}
  </div>
  {('<div style="border-top:1px solid var(--border);padding:1rem 1.5rem"><img src=\"' + (post["screenshot_path"] if post.get("screenshot_path") else "") + '\" style=\"width:100%;border-radius:8px\" alt=\"screenshot\"></div>') if post.get("screenshot_path") and Path(post["screenshot_path"]).exists() else ""}
  <div class="card-footer">
    <span>💬 {post["reply_count"]}</span>
    <span>🔁 {post["renote_count"]}</span>
    <span>✨ {post["reaction_count"]}</span>
    <span class="badge">{post["visibility"]}</span>
    <span style="margin-left:auto">Posted: {(post["created_at"] or "")[:10]}</span>
    <a href="{post["url"]}" target="_blank" rel="noopener">Original ↗</a>
  </div>
</div>
</body></html>"""


# ─── Retake Missing Screenshots ───────────────────────────────────────────────

def retake_screenshots(progress_cb=None) -> dict:
    """Take screenshots for every post that doesn't have one yet."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM posts WHERE screenshot_path IS NULL OR screenshot_path = ''"
        ).fetchall()

    post_ids = [r["id"] for r in rows]
    total    = len(post_ids)
    done     = 0
    failed   = 0

    for post_id in post_ids:
        html = generate_html_mirror_for_screenshot(post_id)
        if html:
            path = take_screenshot(post_id, html)
            if path:
                with get_db() as db:
                    db.execute("UPDATE posts SET screenshot_path=? WHERE id=?",
                               (path, post_id))
                done += 1
            else:
                failed += 1
        else:
            failed += 1

        if progress_cb:
            progress_cb(done + failed, total)

    return {"done": done, "failed": failed, "total": total}


# ─── HTTP Handler ──────────────────────────────────────────────────────────────

_user_archive_progress  = {}  # job_id -> {"done", "total", "status", ...}
_screenshot_job: dict   = {"status": "idle", "done": 0, "total": 0, "failed": 0}


class ArchiveHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, code, html):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p    = urllib.parse.urlparse(self.path)
        qs   = urllib.parse.parse_qs(p.query)
        path = p.path

        if path in ("/", ""):
            self.send_html(200, INDEX_HTML)
        elif path == "/api/posts":
            self._api_list_posts()
        elif path == "/api/progress":
            job_id = qs.get("job", [None])[0]
            self.send_json(200, _user_archive_progress.get(job_id, {"status": "unknown"}))
        elif path == "/api/screenshot-progress":
            self.send_json(200, _screenshot_job)
        elif path.startswith("/post/"):
            self._serve_post_page(urllib.parse.unquote(path[6:]))
        elif path.startswith("/media/"):
            self._serve_media(path[7:])
        elif path.startswith("/download/"):
            self._serve_zip(urllib.parse.unquote(path[10:]))
        elif path.startswith("/screenshot/"):
            self._serve_screenshot(urllib.parse.unquote(path[12:]))
        elif path.startswith("/render/"):
            token = path[8:]
            html  = _render_cache.get(token)
            if html:
                self.send_html(200, html)
            else:
                self.send_response(404); self.end_headers()
        elif path == "/api/playwright-status":
            try:
                import playwright  # noqa
                self.send_json(200, {"available": True})
            except ImportError:
                self.send_json(200, {"available": False})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        path   = urllib.parse.urlparse(self.path).path
        if path == "/api/archive":
            self._api_archive(body)
        elif path == "/api/retake-screenshots":
            self._api_retake_screenshots()
        else:
            self.send_response(404); self.end_headers()

    def _api_retake_screenshots(self):
        global _screenshot_job
        if _screenshot_job.get("status") == "running":
            self.send_json(200, {"status": "already_running"}); return

        # Count how many are missing
        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM posts WHERE screenshot_path IS NULL OR screenshot_path = ''"
            ).fetchone()[0]

        if count == 0:
            self.send_json(200, {"status": "nothing_to_do", "message": "All posts already have screenshots."}); return

        _screenshot_job = {"status": "running", "done": 0, "total": count, "failed": 0}

        def run():
            global _screenshot_job
            def cb(done, total):
                _screenshot_job["done"]  = done
                _screenshot_job["total"] = total
            try:
                result = retake_screenshots(progress_cb=cb)
                _screenshot_job = {"status": "done", **result}
            except Exception as e:
                _screenshot_job = {"status": "error", "error": str(e)}

        threading.Thread(target=run, daemon=True).start()
        self.send_json(200, {"status": "started", "total": count})

    def _api_archive(self, body):
        raw      = body.get("input", "").strip()
        instance = body.get("instance", "").strip()
        max_p    = int(body.get("max_posts", 500))

        if not raw:
            self.send_json(400, {"error": "Input is required."}); return

        try:
            kind, detected_instance, ident = parse_input(raw)

            if detected_instance:
                instance = detected_instance
            if not instance:
                self.send_json(400, {"error": "Instance URL required — include it in the URL/handle, or fill in the Instance field."}); return
            if not instance.startswith("http"):
                instance = "https://" + instance

            if kind == "note":
                result = archive_single(instance, ident)
                self.send_json(200, result)

            elif kind == "user":
                job_id = hashlib.md5(f"{instance}{ident}{datetime.utcnow()}".encode()).hexdigest()[:10]
                _user_archive_progress[job_id] = {"status": "running", "done": 0, "total": 0}

                def run():
                    def cb(done, total):
                        _user_archive_progress[job_id]["done"]  = done
                        _user_archive_progress[job_id]["total"] = total
                    try:
                        result = archive_user(instance, ident, max_posts=max_p, progress_cb=cb)
                        _user_archive_progress[job_id] = {"status": "done", **result}
                    except Exception as e:
                        _user_archive_progress[job_id] = {"status": "error", "error": str(e)}

                threading.Thread(target=run, daemon=True).start()
                self.send_json(200, {"status": "started", "job_id": job_id,
                                     "user": ident, "instance": instance})

        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def _api_list_posts(self):
        with get_db() as db:
            rows = db.execute("""
                SELECT p.id, p.url, p.user_name, p.user_handle, p.user_avatar,
                       p.content, p.cw, p.created_at, p.archived_at,
                       p.reply_count, p.renote_count, p.reaction_count, p.visibility,
                       p.screenshot_path,
                       COUNT(m.id) AS media_count
                FROM posts p
                LEFT JOIN media m ON m.post_id = p.id
                GROUP BY p.id
                ORDER BY p.archived_at DESC
            """).fetchall()
        self.send_json(200, [dict(r) for r in rows])

    def _serve_post_page(self, post_id):
        with get_db() as db:
            _post = db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            if not _post:
                self.send_html(404, "<h1>Post not found</h1>"); return
            post  = dict(_post)
            media = [dict(m) for m in db.execute("SELECT * FROM media WHERE post_id=?", (post_id,)).fetchall()]
        self.send_html(200, generate_html_mirror(post, media, embedded=False))

    def _serve_media(self, rel_path):
        full = MEDIA_DIR / rel_path
        if not full.exists() or not full.is_file():
            self.send_response(404); self.end_headers(); return
        mime, _ = mimetypes.guess_type(str(full))
        data = full.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _serve_zip(self, post_id):
        data = create_zip_for_post(post_id)
        if not data:
            self.send_response(404); self.end_headers(); return
        fname = "sharkey_archive_" + re.sub(r"[^A-Za-z0-9_-]", "_", post_id) + ".zip"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _serve_screenshot(self, post_id):
        with get_db() as db:
            row = db.execute("SELECT screenshot_path FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row or not row["screenshot_path"]:
            self.send_response(404); self.end_headers(); return
        p = Path(row["screenshot_path"])
        if not p.exists():
            self.send_response(404); self.end_headers(); return
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)


# ─── Frontend ─────────────────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sharkey Archiver</title>
<style>
  :root {
    --bg:#0f1117; --card:#1a1d2e; --card2:#1f2235;
    --border:#2d3154; --accent:#7c6ff7; --accent2:#a78bfa;
    --text:#e2e4ef; --muted:#8b8fa8;
    --success:#4ade80; --danger:#f87171; --warning:#f59e0b;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;min-height:100vh}

  header{background:var(--card);border-bottom:1px solid var(--border);padding:1rem 2rem;display:flex;align-items:center;gap:1rem}
  header h1{font-size:1.4rem;font-weight:800;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .subtitle{color:var(--muted);font-size:.85rem}

  .layout{display:grid;grid-template-columns:380px 1fr;min-height:calc(100vh - 65px)}
  .sidebar{background:var(--card2);border-right:1px solid var(--border);padding:1.5rem;display:flex;flex-direction:column;gap:1.25rem;position:sticky;top:0;height:calc(100vh - 65px);overflow-y:auto}
  .main{padding:1.5rem;overflow-y:auto}

  .form-group{display:flex;flex-direction:column;gap:.4rem}
  label{font-size:.75rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
  input,select{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:.9rem;padding:.6rem .85rem;width:100%;transition:border-color .2s}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  input::placeholder{color:var(--muted)}
  select option{background:var(--card)}

  .hint{font-size:.75rem;color:var(--muted);margin-top:.3rem;line-height:1.6;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px;padding:.6rem .75rem}
  .hint code{color:var(--accent2);font-size:.72rem}

  .btn{display:flex;align-items:center;justify-content:center;gap:.5rem;padding:.7rem 1.2rem;border-radius:8px;border:none;cursor:pointer;font-size:.9rem;font-weight:600;transition:all .2s;width:100%}
  .btn-primary{background:var(--accent);color:#fff}
  .btn-primary:hover{background:var(--accent2);transform:translateY(-1px)}
  .btn-primary:disabled{opacity:.5;cursor:not-allowed;transform:none}

  #status{font-size:.85rem;border-radius:8px;padding:.6rem .9rem;display:none;margin-top:.25rem}
  #status.success{background:rgba(74,222,128,.1);border:1px solid var(--success);color:var(--success);display:block}
  #status.error  {background:rgba(248,113,113,.1);border:1px solid var(--danger);color:var(--danger);display:block}
  #status.info   {background:rgba(124,111,247,.1);border:1px solid var(--accent);color:var(--accent2);display:block}
  #status.warning{background:rgba(245,158,11,.1);border:1px solid var(--warning);color:var(--warning);display:block}

  .progress-bar-wrap{background:var(--border);border-radius:99px;height:6px;overflow:hidden;display:none}
  .progress-bar{height:100%;background:var(--accent);transition:width .4s;border-radius:99px}

  hr{border:none;border-top:1px solid var(--border)}

  .section-title{font-size:.75rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem}
  .count{background:var(--border);border-radius:99px;padding:.1rem .5rem;font-size:.72rem}

  .post-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}
  .post-card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:border-color .2s,transform .2s}
  .post-card:hover{border-color:var(--accent);transform:translateY(-2px)}
  .pc-header{padding:1rem;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:.75rem}
  .av{width:38px;height:38px;border-radius:50%;background:var(--border);object-fit:cover;flex-shrink:0}
  .pc-name{font-weight:700;font-size:.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pc-handle{color:var(--muted);font-size:.75rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pc-body{padding:1rem}
  .pc-cw{font-size:.78rem;color:var(--warning);margin-bottom:.4rem;font-style:italic}
  .pc-text{font-size:.85rem;line-height:1.6;max-height:90px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}
  .pc-text.empty{color:var(--muted);font-style:italic}
  .badges{display:flex;gap:.35rem;flex-wrap:wrap;margin-top:.6rem}
  .badge{font-size:.7rem;padding:.15rem .5rem;border-radius:99px;border:1px solid}
  .bm{border-color:var(--accent);color:var(--accent2)}
  .bv{border-color:var(--border);color:var(--muted)}
  .pc-footer{padding:.7rem 1rem;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
  .pc-stats{display:flex;gap:.75rem;font-size:.75rem;color:var(--muted)}
  .pc-actions{display:flex;gap:.4rem}
  .btn-sm{padding:.3rem .6rem;border-radius:6px;font-size:.75rem;font-weight:600;border:none;cursor:pointer;transition:all .15s}
  .btn-view{background:rgba(124,111,247,.15);color:var(--accent2)}
  .btn-view:hover{background:rgba(124,111,247,.3)}
  .btn-dl{background:rgba(74,222,128,.1);color:var(--success)}
  .btn-dl:hover{background:rgba(74,222,128,.2)}

  .empty-state{grid-column:1/-1;text-align:center;padding:4rem 2rem;color:var(--muted)}
  .empty-state .icon{font-size:3rem;margin-bottom:1rem}

  .spinner{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  @media(max-width:768px){.layout{grid-template-columns:1fr}.sidebar{position:static;height:auto}.post-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<header>
  <div>
    <h1>🦈 Sharkey Archiver</h1>
    <div class="subtitle">Archive public posts from any Sharkey or Misskey instance — no API key needed</div>
  </div>
</header>

<div class="layout">
<aside class="sidebar">

  <div class="form-group">
    <label>Post URL, user profile, or handle</label>
    <input id="inp" type="text" placeholder="Paste a URL or @user@instance…">
    <div class="hint">
      <strong>Single post:</strong><br>
      <code>https://example.social/notes/abc123</code><br><br>
      <strong>All posts by a user:</strong><br>
      <code>https://example.social/@alice</code><br>
      <code>@alice@example.social</code><br>
      <code>alice</code> &nbsp;(fill Instance below)
    </div>
  </div>

  <div class="form-group">
    <label>Instance <span style="font-weight:400;text-transform:none;font-size:.7rem">(auto-detected from URL / handle)</span></label>
    <input id="instance" type="text" placeholder="https://sharkey.example.com">
  </div>

  <div class="form-group">
    <label>Max posts (for user archive)</label>
    <select id="maxposts">
      <option value="100">100</option>
      <option value="250">250</option>
      <option value="500" selected>500</option>
      <option value="1000">1000</option>
      <option value="9999">All</option>
    </select>
  </div>

  <button class="btn btn-primary" id="go-btn" onclick="doArchive()">Archive</button>
  <div id="status"></div>
  <div class="progress-bar-wrap" id="pbwrap"><div class="progress-bar" id="pb" style="width:0%"></div></div>

  <hr>
  <div style="font-size:.75rem;color:var(--muted);line-height:1.7">
    <strong style="color:var(--text)">No API key required.</strong><br>
    Only public posts can be archived without authentication.
    Media (images, video, audio) is downloaded and stored locally.
  </div>

  <div id="pw-notice" style="font-size:.75rem;line-height:1.6;background:rgba(245,158,11,.08);border:1px solid #f59e0b;border-radius:8px;padding:.65rem .85rem;color:#f59e0b;display:none">
    <strong>Screenshots disabled.</strong><br>
    Install Playwright to enable automatic screenshots:<br>
    <code style="user-select:all;color:#fcd34d">pip install playwright</code><br>
    <code style="user-select:all;color:#fcd34d">playwright install chromium</code>
  </div>

  <div id="retake-section" style="display:none">
    <button class="btn btn-primary" id="retake-btn" onclick="retakeScreenshots()"
      style="background:rgba(124,111,247,.2);color:var(--accent2);border:1px solid var(--accent)">
      📷 Screenshot Missing Posts
    </button>
    <div id="retake-status" style="font-size:.8rem;color:var(--muted);margin-top:.4rem;display:none"></div>
    <div class="progress-bar-wrap" id="retake-pbwrap" style="margin-top:.4rem">
      <div class="progress-bar" id="retake-pb" style="width:0%;background:#a78bfa"></div>
    </div>
  </div>

</aside>

<main class="main">
  <div class="section-title">Archived Posts <span class="count" id="post-count">0</span></div>
  <div class="post-grid" id="grid">
    <div class="empty-state" id="empty">
      <div class="icon">📦</div>
      <p>No posts archived yet.<br>Paste a post URL or user profile in the sidebar.</p>
    </div>
  </div>
</main>
</div>

<script>
let pollTimer = null;

function setStatus(type, msg) {
  const el = document.getElementById('status');
  el.className = type; el.textContent = msg; el.style.display = 'block';
}

async function doArchive() {
  const inp      = document.getElementById('inp').value.trim();
  const instance = document.getElementById('instance').value.trim();
  const maxPosts = document.getElementById('maxposts').value;
  const btn      = document.getElementById('go-btn');
  if (!inp) { setStatus('error', 'Please enter a URL, handle, or username.'); return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Working…';
  setStatus('info', 'Sending request…');

  try {
    const res  = await fetch('/api/archive', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({input: inp, instance, max_posts: parseInt(maxPosts)})
    });
    const data = await res.json();

    if (!res.ok) {
      setStatus('error', data.error || 'Unknown error');
    } else if (data.status === 'already_archived') {
      setStatus('warning', '✓ Already in archive.'); loadPosts();
    } else if (data.status === 'archived') {
      setStatus('success', '✓ Post archived!'); loadPosts();
    } else if (data.status === 'started') {
      setStatus('info', `Archiving @${data.user}@${new URL(data.instance).hostname}…`);
      document.getElementById('pbwrap').style.display = 'block';
      pollProgress(data.job_id);
      return;
    }
  } catch(e) {
    setStatus('error', 'Network error: ' + e.message);
  }

  btn.disabled = false;
  btn.innerHTML = 'Archive';
}

function pollProgress(jobId) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const res  = await fetch('/api/progress?job=' + jobId);
    const data = await res.json();
    if (data.status === 'running') {
      const pct = data.total > 0 ? Math.round(data.done / data.total * 100) : 0;
      document.getElementById('pb').style.width = pct + '%';
      setStatus('info', `Archiving… ${data.done} saved (${data.total} fetched so far)`);
      pollProgress(jobId);
    } else if (data.status === 'done') {
      document.getElementById('pb').style.width = '100%';
      setStatus('success', `✓ Done! ${data.archived} new posts archived, ${data.skipped} already existed.`);
      document.getElementById('pbwrap').style.display = 'none';
      document.getElementById('go-btn').disabled = false;
      document.getElementById('go-btn').innerHTML = 'Archive';
      loadPosts();
    } else if (data.status === 'error') {
      setStatus('error', data.error || 'Archive failed.');
      document.getElementById('pbwrap').style.display = 'none';
      document.getElementById('go-btn').disabled = false;
      document.getElementById('go-btn').innerHTML = 'Archive';
    } else {
      pollProgress(jobId);
    }
  }, 800);
}

document.getElementById('inp').addEventListener('keydown', e => {
  if (e.key === 'Enter') doArchive();
});

async function loadPosts() {
  const res   = await fetch('/api/posts');
  const posts = await res.json();
  document.getElementById('post-count').textContent = posts.length;
  const grid = document.getElementById('grid');
  grid.querySelectorAll('.post-card').forEach(c => c.remove());
  document.getElementById('empty').style.display = posts.length ? 'none' : '';

  for (const p of posts) {
    const card = document.createElement('div');
    card.className = 'post-card';
    const av = p.user_avatar
      ? `<img class="av" src="${esc(p.user_avatar)}" alt="" loading="lazy" onerror="this.style.display='none'">`
      : `<div class="av"></div>`;
    const txt = p.content
      ? `<div class="pc-text">${esc(p.content).replace(/\\n/g,'<br>')}</div>`
      : `<div class="pc-text empty">[No text]</div>`;
    const cw = p.cw ? `<div class="pc-cw">⚠ ${esc(p.cw)}</div>` : '';
    const mb = p.media_count > 0
      ? `<span class="badge bm">📎 ${p.media_count} file${p.media_count>1?'s':''}</span>` : '';
    const eid = encodeURIComponent(p.id);
    card.innerHTML = `
      <div class="pc-header">${av}<div style="min-width:0">
        <div class="pc-name">${esc(p.user_name||p.user_handle)}</div>
        <div class="pc-handle">${esc(p.user_handle)}</div>
      </div></div>
      <div class="pc-body">${cw}${txt}
        <div class="badges">${mb}<span class="badge bv">${esc(p.visibility)}</span>${p.screenshot_path ? '<span class="badge" style="border-color:#4ade80;color:#4ade80">📷 screenshot</span>' : ''}</div>
        ${p.screenshot_path ? `<img src="/screenshot/${eid}" style="width:100%;border-radius:8px;margin-top:.6rem;display:block" loading="lazy" alt="screenshot">` : ''}
      </div>
      <div class="pc-footer">
        <div class="pc-stats"><span>💬${p.reply_count}</span><span>🔁${p.renote_count}</span><span>✨${p.reaction_count}</span></div>
        <div class="pc-actions">
          <button class="btn-sm btn-view" onclick="window.open('/post/${eid}','_blank')">View</button>
          <button class="btn-sm btn-dl"   onclick="location.href='/download/${eid}'">⬇ ZIP</button>
        </div>
      </div>`;
    grid.appendChild(card);
  }
}

function esc(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadPosts();
checkPlaywright();

async function checkPlaywright() {
  try {
    const res  = await fetch('/api/playwright-status');
    const data = await res.json();
    if (!data.available) {
      document.getElementById('pw-notice').style.display = 'block';
    } else {
      // Show retake button only if Playwright is available
      document.getElementById('retake-section').style.display = 'block';
    }
  } catch(e) {}
}

let retakePollTimer = null;

async function retakeScreenshots() {
  const btn = document.getElementById('retake-btn');
  const st  = document.getElementById('retake-status');
  const pbw = document.getElementById('retake-pbwrap');

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Starting…';
  st.style.display = 'block';
  st.textContent = 'Sending request…';

  try {
    const res  = await fetch('/api/retake-screenshots', { method: 'POST' });
    const data = await res.json();

    if (data.status === 'nothing_to_do') {
      st.textContent = '✓ All posts already have screenshots.';
      btn.disabled = false;
      btn.innerHTML = '📷 Screenshot Missing Posts';
      return;
    } else if (data.status === 'already_running') {
      st.textContent = 'Already running, please wait…';
      pollRetakeProgress();
      return;
    } else if (data.status === 'started') {
      st.textContent = `Taking screenshots for ${data.total} posts…`;
      pbw.style.display = 'block';
      pollRetakeProgress();
      return;
    }
  } catch(e) {
    st.textContent = 'Error: ' + e.message;
    btn.disabled = false;
    btn.innerHTML = '📷 Screenshot Missing Posts';
  }
}

function pollRetakeProgress() {
  clearTimeout(retakePollTimer);
  retakePollTimer = setTimeout(async () => {
    const res  = await fetch('/api/screenshot-progress');
    const data = await res.json();
    const st   = document.getElementById('retake-status');
    const pb   = document.getElementById('retake-pb');
    const pbw  = document.getElementById('retake-pbwrap');
    const btn  = document.getElementById('retake-btn');

    if (data.status === 'running') {
      const pct = data.total > 0 ? Math.round(data.done / data.total * 100) : 0;
      pb.style.width = pct + '%';
      st.textContent = `${data.done} / ${data.total} screenshotted…`;
      pollRetakeProgress();
    } else if (data.status === 'done') {
      pb.style.width = '100%';
      st.textContent = `✓ Done! ${data.done} screenshots taken, ${data.failed} failed.`;
      pbw.style.display = 'none';
      btn.disabled = false;
      btn.innerHTML = '📷 Screenshot Missing Posts';
      loadPosts();
    } else if (data.status === 'error') {
      st.textContent = 'Error: ' + (data.error || 'unknown');
      btn.disabled = false;
      btn.innerHTML = '📷 Screenshot Missing Posts';
    } else if (data.status === 'idle') {
      // Job finished before we polled — reload to show results
      btn.disabled = false;
      btn.innerHTML = '📷 Screenshot Missing Posts';
      loadPosts();
    }
  }, 800);
}
</script>
</body>
</html>"""


# ─── Entry Point ──────────────────────────────────────────────────────────────

def check_and_install_playwright():
    """
    Check if Playwright is installed and functional. If not, offer to auto-install.
    Returns True if available (or successfully installed), False if user declines.
    """
    try:
        from playwright.sync_api import sync_playwright
        # Also verify Chromium is actually downloaded
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except ImportError:
        # Playwright package not installed
        print("\n" + "="*60)
        print("📷 PLAYWRIGHT NOT INSTALLED")
        print("="*60)
        print("\nPlaywright is required for automatic post screenshots.")
        print("Without it, the archiver still works — screenshots are just skipped.\n")
        response = input("Install Playwright now? [Y/n]: ").strip().lower()
        if response in ("", "y", "yes"):
            print("\nInstalling Playwright (this may take a minute)...")
            import subprocess, sys
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                print("✓ Playwright installed")
                print("\nDownloading Chromium browser (~150 MB)...")
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                print("✓ Chromium installed\n")
                print("Screenshots are now enabled!\n")
                return True
            except subprocess.CalledProcessError as e:
                print(f"\n✗ Installation failed: {e}")
                print("You can install manually later with:")
                print("  pip install playwright")
                print("  python -m playwright install chromium\n")
                return False
        else:
            print("\nSkipping installation. You can install later with:")
            print("  pip install playwright")
            print("  python -m playwright install chromium\n")
            return False
    except Exception as e:
        # Playwright installed but Chromium not downloaded
        print("\n" + "="*60)
        print("📷 CHROMIUM BROWSER NOT FOUND")
        print("="*60)
        print(f"\nPlaywright is installed, but Chromium hasn't been downloaded yet.")
        print("This is needed for automatic screenshots.\n")
        response = input("Download Chromium now? [Y/n]: ").strip().lower()
        if response in ("", "y", "yes"):
            print("\nDownloading Chromium (~150 MB, one-time only)...")
            import subprocess, sys
            try:
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                print("✓ Chromium installed\n")
                print("Screenshots are now enabled!\n")
                return True
            except subprocess.CalledProcessError:
                print("\n✗ Download failed. Try manually:")
                print("  python -m playwright install chromium\n")
                return False
        else:
            print("\nSkipping download. You can install later with:")
            print("  python -m playwright install chromium\n")
            return False


def find_free_port(start=5757):
    import socket
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port)); return port
            except OSError:
                continue
    return start


def get_data_dir():
    import sys
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    d = base / "archive_data"
    d.mkdir(parents=True, exist_ok=True)
    return d


if __name__ == "__main__":
    import sys, webbrowser

    # Check/install Playwright before anything else
    # (only prompts if missing — instant pass-through if already installed)
    check_and_install_playwright()

    data_dir = get_data_dir()

    # Redirect DB and media to the persistent data dir.
    # We update the module globals dict directly so every function picks up
    # the new paths at call time — works for both .py and PyInstaller .exe.
    import sys as _sys
    _mod = _sys.modules[__name__]
    _mod.DB_PATH   = str(data_dir / "archive.db")
    _mod.MEDIA_DIR = data_dir / "media"
    _mod.MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    port = find_free_port(5757)
    url  = f"http://localhost:{port}"

    # Make the port available to take_screenshot
    import sys as _sys
    _sys.modules[__name__]._server_port = port

    init_db()

    server = HTTPServer(("127.0.0.1", port), ArchiveHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def _open():
        import time; time.sleep(0.4); webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()

    try:
        import tkinter as tk
        root = tk.Tk(); root.withdraw(); root.title("Sharkey Archiver")
        win = tk.Toplevel(root)
        win.title("🦈 Sharkey Archiver"); win.resizable(False, False); win.geometry("320x130")
        try: win.attributes("-topmost", True)
        except: pass
        tk.Label(win, text="🦈 Sharkey Archiver is running", font=("Segoe UI", 11, "bold"), pady=10).pack()
        tk.Label(win, text=url, fg="blue", font=("Segoe UI", 9)).pack()
        tk.Label(win, text="Close this window to stop the app.", font=("Segoe UI", 8), fg="grey").pack(pady=4)
        tk.Button(win, text="Open in Browser", command=lambda: webbrowser.open(url), font=("Segoe UI", 9)).pack(pady=2)
        win.protocol("WM_DELETE_WINDOW", lambda: (server.shutdown(), root.destroy()))
        root.mainloop()
    except Exception:
        try: threading.Event().wait()
        except KeyboardInterrupt: server.shutdown()
