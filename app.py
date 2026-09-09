import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for, flash
from PIL import Image, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0 Safari/537.36"
)
REQUEST_TIMEOUT = (8, 30)
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_IMAGES = 300
MIN_WIDTH = 200
MIN_HEIGHT = 200
IMAGE_WORKERS = 12
SERPAPI_API_URL = "https://serpapi.com/search.json"
SERPAPI_API_KEY = os.environ.get("SERPAPI_API_KEY", "").strip()
PIXABAY_API_URL = "https://pixabay.com/api/"
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
SEARCH_PAGE_SIZE = 10

CATEGORIES = [
    {"name": "Nature", "count": "12,543", "image": "nature.jpg"},
    {"name": "City", "count": "9,876", "image": "city.jpg"},
    {"name": "abstract", "count": "8,231", "image": "abstract.jpg"},
    {"name": "technology", "count": "7,654", "image": "technology.jpg"},
    {"name": "Space", "count": "6,789", "image": "space.jpg"},
    {"name": "animal", "count": "5,432", "image": "animal.jpg"},
]

FEATURED = [
    {"name": "Cyberpunk City", "image": "cyberpunk.jpg"},
    {"name": "Futurescape", "image": "future.jpg"},
    {"name": "Animation", "image": "Animation.jpg"},
    {"name": "Digital World", "image": "technology.jpg"},
    {"name": "Beyond Space", "image": "space.jpg"},
]

TRENDING = [
    {"name": "Cyberpunk City", "downloads": "2,345", "image": "cyberpunk.jpg"},
    {"name": "Animation", "downloads": "1,987", "image": "Animation.jpg"},
    {"name": "Futurescape", "downloads": "1,765", "image": "future.jpg"},
    {"name": "Digital Art", "downloads": "1,432", "image": "abstract.jpg"},
]

BLOCKED_KEYWORDS = {
    "logo", "icon", "favicon", "avatar", "banner", "sprite", "placeholder",
    "tracking", "emoji", "badge", "button", "loader", "spinner"
}
THUMB_PATH_PATTERNS = [
    (r"/thumbs?/", "/"), (r"/thumbnail(s)?/", "/"), (r"/small/", "/"),
    (r"/medium/", "/"), (r"/tiny/", "/"), (r"/preview/", "/"),
    (r"/previews?/", "/"), (r"/\d{2,4}x\d{2,4}/", "/"),
    (r"/\d{2,4}x/", "/"), (r"/x\d{2,4}/", "/"),
]
RESIZE_QUERY_KEYS = {
    "w", "width", "h", "height", "mw", "mh", "maxwidth", "maxheight",
    "size", "resize", "res", "quality", "q", "fit", "crop", "scale",
    "format", "fm", "dpr"
}
UI_CONTEXT_WORDS = {
    "header", "footer", "navbar", "nav", "navigation", "menu", "sidebar",
    "aside", "breadcrumb", "logo", "brand", "social", "share", "related",
    "recommend", "recommended", "advert", "ads", "ad-container", "sponsor",
    "video", "videos", "player", "media-player", "modal", "popup", "cookie",
    "login", "account", "profile", "comment", "comments", "toolbar"
}
GALLERY_CONTEXT_WORDS = {
    "gallery", "galleries", "photos", "photo-gallery", "images", "image-gallery",
    "album", "albums", "folder", "files", "grid", "lightbox", "masonry",
    "thumbnails", "thumbnail-grid", "photo-grid", "media-grid"
}
BLOCKED_URL_WORDS = {"logo", "favicon", "sprite", "placeholder", "tracking", "emoji", "loader", "spinner", "avatar", "icon"}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                search_type TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wishlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                image_url TEXT NOT NULL,
                title TEXT,
                thumbnail_url TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, image_url),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                image_url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def is_public_host(hostname: str) -> bool:
    if not hostname:
        return False
    host = hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def validate_url(raw_url: str) -> str:
    raw_url = (raw_url or "").strip()
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")
    if not parsed.hostname or not is_public_host(parsed.hostname):
        raise ValueError("The target host is not a public Internet host.")
    return raw_url


def safe_urljoin(base: str, value: str):
    if not value:
        return None
    value = value.strip()
    if value.startswith(("data:", "blob:", "javascript:", "#")):
        return None
    absolute = urljoin(base, value)
    try:
        return validate_url(absolute)
    except ValueError:
        return None


def clean_image_url(image_url: str) -> str:
    parsed = urlparse(image_url)
    path = parsed.path
    for pattern, replacement in THUMB_PATH_PATTERNS:
        path = re.sub(pattern, replacement, path, flags=re.IGNORECASE)
    path = re.sub(r"([_-])\d{2,4}x\d{2,4}(?=\.[A-Za-z0-9]{2,5}$)", "", path)
    path = re.sub(r"([_-])\d{2,4}x(?=\.[A-Za-z0-9]{2,5}$)", "", path)
    path = re.sub(r"@(?:1|1x|2|2x|3|3x)(?=\.[A-Za-z0-9]{2,5}$)", "", path)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in RESIZE_QUERY_KEYS]
    return urlunparse(parsed._replace(path=path, query=urlencode(query, doseq=True)))


def parse_srcset(srcset: str):
    parsed = []
    for item in srcset.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split()
        descriptor = 0
        if len(parts) > 1:
            m = re.match(r"([0-9.]+)(w|x)$", parts[1])
            if m:
                descriptor = float(m.group(1))
        parsed.append((descriptor, parts[0]))
    parsed.sort(key=lambda x: x[0])
    return [url for _, url in parsed]


def context_text(tag) -> str:
    parts = []
    for node in [tag] + list(tag.parents)[:4]:
        if not getattr(node, "name", None):
            continue
        parts.append(node.name)
        parts.extend(str(node.get("id", "")).split())
        classes = node.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        parts.extend(classes)
    return " ".join(parts).lower()


def has_blocked_context(tag):
    return any(word in context_text(tag) for word in UI_CONTEXT_WORDS)


def has_gallery_context(tag):
    return any(word in context_text(tag) for word in GALLERY_CONTEXT_WORDS)


def inside_non_photo_media(tag):
    for parent in [tag] + list(tag.parents)[:6]:
        if not getattr(parent, "name", None):
            continue
        if parent.name in {"video", "source", "track", "iframe"}:
            return True
        raw = f"{parent.get('id', '')} {' '.join(parent.get('class', []) if isinstance(parent.get('class', []), list) else [str(parent.get('class', ''))])}".lower()
        if any(word in raw for word in {"video", "videoplayer", "video-player", "youtube", "vimeo"}):
            return True
    return False


def looks_like_ui_asset(url: str, alt: str = ""):
    haystack = f"{url} {alt}".lower()
    return any(word in haystack for word in BLOCKED_URL_WORDS)


def best_original_url(img, page_url: str):
    attrs = (
        "data-original", "data-full", "data-full-src", "data-fullsize", "data-image",
        "data-image-url", "data-download", "data-src-original", "data-hires",
        "data-highres", "data-large", "data-zoom-image"
    )
    for attr in attrs:
        value = img.get(attr)
        if value:
            absolute = safe_urljoin(page_url, value)
            if absolute:
                return absolute
    source_candidates = []
    picture = img.find_parent("picture")
    if picture:
        for source in picture.find_all("source"):
            srcset = source.get("srcset") or source.get("data-srcset")
            if srcset:
                source_candidates.extend(parse_srcset(srcset))
    srcset = img.get("data-srcset") or img.get("srcset")
    if srcset:
        source_candidates.extend(parse_srcset(srcset))
    if source_candidates:
        absolute = safe_urljoin(page_url, source_candidates[-1])
        if absolute:
            return absolute
    for attr in ("data-src", "data-lazy-src", "src"):
        value = img.get(attr)
        if value:
            absolute = safe_urljoin(page_url, value)
            if absolute:
                return absolute
    return None


def gallery_ancestors(soup):
    scores = {}
    for tag in soup.find_all(["section", "div", "main", "article", "ul", "ol"]):
        images = tag.find_all("img")
        if not images:
            continue
        context = context_text(tag)
        score = min(len(images), 40) * 2
        if any(w in context for w in GALLERY_CONTEXT_WORDS):
            score += 50
        if any(w in context for w in UI_CONTEXT_WORDS):
            score -= 45
        if len(images) >= 4:
            score += 20
        elif len(images) == 1:
            score -= 15
        scores[tag] = score
    return [tag for tag, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:12]]


def image_in_gallery(img, gallery_set):
    if img in gallery_set:
        return True
    for ancestor in img.parents:
        if ancestor in gallery_set:
            return True
        if ancestor.name in {"main", "body", "html"}:
            break
    return False


def extract_image_candidates(page_url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    candidates = {}
    gallery_containers = gallery_ancestors(soup)
    gallery_set = set(gallery_containers)

    def add(raw_url, img, source_url=None, link_text=""):
        absolute = safe_urljoin(page_url, raw_url)
        if not absolute or looks_like_ui_asset(absolute, img.get("alt", "")) or inside_non_photo_media(img):
            return
        if has_blocked_context(img) and not has_gallery_context(img):
            return
        target = clean_image_url(absolute)
        candidates.setdefault(target, {
            "url": target,
            "source_url": source_url or absolute,
            "alt": (img.get("alt") or link_text or "")[:200],
            "declared_width": int(img.get("width")) if str(img.get("width", "")).isdigit() else None,
            "declared_height": int(img.get("height")) if str(img.get("height", "")).isdigit() else None,
        })

    for img in soup.find_all("img"):
        if gallery_containers and not image_in_gallery(img, gallery_set):
            continue
        if has_blocked_context(img) and not has_gallery_context(img):
            continue
        original = best_original_url(img, page_url)
        if not original:
            continue
        anchor = img.find_parent("a", href=True)
        if anchor:
            href = safe_urljoin(page_url, anchor.get("href"))
            if href and not looks_like_ui_asset(href, anchor.get_text(" ", strip=True)):
                path = urlparse(href).path.lower()
                if re.search(r"\.(jpe?g|png|webp|gif|avif|tiff?|bmp)(?:$|[?#])", path):
                    original = href
        add(original, img, source_url=original, link_text=anchor.get_text(" ", strip=True) if anchor else "")
    return list(candidates.values())[:MAX_IMAGES]


def fetch_bytes(url: str, referer=None, max_bytes=MAX_IMAGE_BYTES):
    validate_url(url)
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    with requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as response:
        response.raise_for_status()
        validate_url(response.url)
        content_type = response.headers.get("Content-Type", "").lower()
        if not (content_type.startswith("image/") or content_type in {"application/octet-stream", ""}):
            raise ValueError("Resource is not an image.")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise ValueError("Image is too large.")
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Image exceeds the configured size limit.")
            chunks.append(chunk)
        return b"".join(chunks), response.url, content_type


def inspect_image(item: dict, referer=None):
    try:
        data, final_url, _ = fetch_bytes(item["url"], referer)
        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = im.size
                fmt = (im.format or "").upper()
        except (UnidentifiedImageError, OSError):
            return None
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return None
        return {"url": final_url, "source_url": item.get("source_url", item["url"]), "width": width,
                "height": height, "format": fmt, "bytes": len(data), "alt": item.get("alt", ""), "preview_url": final_url}
    except Exception:
        return None


def filename_for(url: str, index: int, content: bytes):
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or f"image_{index}"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]
    if "." not in name:
        try:
            with Image.open(io.BytesIO(content)) as im:
                ext = (im.format or "jpg").lower()
        except Exception:
            ext = "jpg"
        name += f".{ext}"
    return f"{index:03d}_{name}"



def serpapi_image_search(query: str, start: int = 1, num: int = SEARCH_PAGE_SIZE):
    """Search Google Images through SerpAPI. Returns image URLs only, with no
    description, tags, titles, news, videos, or normal web-search results."""
    if not SERPAPI_API_KEY:
        raise RuntimeError("Image search is not configured. Set SERPAPI_API_KEY in your .env file.")

    start = max(1, int(start))
    num = min(20, max(1, int(num)))
    page_index = (start - 1) // num
    params = {
        "engine": "google_images",
        "q": query,
        "api_key": SERPAPI_API_KEY,
        "ijn": page_index,
        "safe": "active",
        "hl": "en",
    }

    try:
        r = requests.get(
            SERPAPI_API_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Image search failed: {exc}") from exc

    if not r.ok:
        try:
            data = r.json()
            detail = data.get("error", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"Image search failed: {detail}")

    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Image search failed: {data['error']}")

    images = []
    seen = set()

    for item in data.get("images_results", []):
        image_url = (item.get("original") or "").strip()
        preview_url = (item.get("thumbnail") or image_url).strip()
        if not image_url or not preview_url or image_url in seen:
            continue
        seen.add(image_url)

        # Deliberately do not return title, tags, description, source text,
        # or any other caption data. The frontend receives image URLs only.
        images.append({
            "url": image_url,
            "preview_url": preview_url,
            "title": "",
            "source": "",
            "width": item.get("original_width"),
            "height": item.get("original_height"),
        })
        if len(images) >= num:
            break

    # Google Images commonly returns a full page of results. Continue while
    # SerpAPI supplied image results for the requested page.
    has_more = len(images) >= num
    return {
        "images": images,
        "has_more": has_more,
        "next_start": start + num,
        "total": None,
    }


def universal_search(query: str, start: int = 1):
    # Keyword searches are image-only. SerpAPI is used as the image provider.
    return serpapi_image_search(query, start, SEARCH_PAGE_SIZE)


def detect_search_mode(value: str):
    parsed = urlparse((value or '').strip())
    return "url" if parsed.scheme in {"http", "https"} and parsed.netloc else "keyword"

def save_search(query: str, search_type: str):
    if "user_id" not in session:
        return
    with get_db() as conn:
        conn.execute("INSERT INTO search_history(user_id, query, search_type) VALUES(?,?,?)", (session["user_id"], query, search_type))
        conn.commit()

@app.context_processor
def inject_globals():
    return {"current_user": session.get("user_name"), "current_user_id": session.get("user_id")}


@app.get("/")
def index():
    return render_template("home.html", categories=CATEGORIES, featured=FEATURED, trending=TRENDING)

@app.get("/category/<slug>")
def category(slug):
    match = next((c for c in CATEGORIES if c["name"].lower() == slug.lower()), None)
    if not match:
        return redirect(url_for("index"))
    try:
        result = universal_search(match["name"], 1)
    except RuntimeError as exc:
        result = {
            "images": [],
            "has_more": False,
            "next_start": SEARCH_PAGE_SIZE + 1,
            "error": str(exc),
        }

    # Category searches are image-only. Do not pass web/news/video fields.
    return render_template(
        "results.html",
        title=f"Images: {match['name']}",
        query=match["name"],
        images=result.get("images", []),
        next_start=result.get("next_start", SEARCH_PAGE_SIZE + 1),
        has_more=result.get("has_more", False),
        error=result.get("error"),
        mode="keyword",
    )

@app.get("/search")
def search_page():
    query = request.args.get("q", "").strip()
    if not query:
        return redirect(url_for("index"))
    mode = detect_search_mode(query)
    if mode == "url":
        return redirect(url_for("analysis_page", target=query))
    try:
        result = universal_search(query, 1)
        save_search(query, mode)
    except RuntimeError as exc:
        result = {
            "images": [],
            "has_more": False,
            "next_start": SEARCH_PAGE_SIZE + 1,
            "error": str(exc),
        }
    return render_template("results.html", title=f"Images: {query}", query=query, images=result["images"], next_start=result["next_start"], has_more=result["has_more"], error=result.get("error"), mode=mode)

@app.get("/analysis")
def analysis_page():
    target = request.args.get("target", "").strip()
    if not target:
        return redirect(url_for("index"))
    return render_template("analysis.html", target=target)

@app.get("/login")
def login():
    if "user_id" in session:
        return redirect(url_for("account"))
    return render_template("login.html")

@app.post("/login")
def login_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    next_url = request.form.get("next") or url_for("account")
    if not email or not password:
        flash("Enter your email and password.", "error")
        return redirect(url_for("login", next=next_url))
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        flash("Invalid email or password.", "error")
        return redirect(url_for("login", next=next_url))
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    flash(f"Welcome back, {user['name']}!", "success")
    return redirect(next_url if next_url.startswith("/") else url_for("account"))

@app.get("/register")
def register():
    if "user_id" in session:
        return redirect(url_for("account"))
    return render_template("register.html")

@app.post("/register")
def register_post():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    if len(name) < 2 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        flash("Please enter a valid name and email.", "error")
        return redirect(url_for("register"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("register"))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("register"))
    try:
        with get_db() as conn:
            cur = conn.execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)", (name, email, generate_password_hash(password)))
            conn.commit()
            user_id = cur.lastrowid
    except sqlite3.IntegrityError:
        flash("An account with that email already exists.", "error")
        return redirect(url_for("register"))
    session["user_id"] = user_id
    session["user_name"] = name
    flash("Account created successfully.", "success")
    return redirect(url_for("account"))

@app.get("/account")
@login_required
def account():
    uid = session["user_id"]
    with get_db() as conn:
        user = conn.execute("SELECT id,name,email,created_at FROM users WHERE id=?", (uid,)).fetchone()
        history = conn.execute("SELECT query,search_type,created_at FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
        wishlist = conn.execute("SELECT image_url,title,thumbnail_url,created_at FROM wishlist WHERE user_id=? ORDER BY id DESC", (uid,)).fetchall()
        downloads = conn.execute("SELECT image_url,created_at FROM download_history WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)).fetchall()
    return render_template("account.html", user=user, history=history, wishlist=wishlist, downloads=downloads)

@app.get("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))

@app.get("/about")
def about():
    return render_template("simple.html", title="About Image Downloader Hub", subtitle="Fast, secure, high-quality image discovery from public webpages.", active="about")

@app.get("/contact")
def contact():
    return render_template("simple.html", title="Contact", subtitle="For support, product feedback, or project questions, use your preferred support channel.", active="contact")

@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    start = request.args.get("start", 1, type=int)
    if not query:
        return jsonify({"error": "Enter a URL or keyword."}), 400
    mode = detect_search_mode(query)
    if mode == "url":
        return jsonify({"mode": "url", "redirect": url_for("analysis_page", target=query)})
    try:
        result = universal_search(query, start)
        if start == 1:
            save_search(query, mode)
        return jsonify({"mode": mode, "query": query, **result})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

@app.get("/api/category/<slug>")
def api_category(slug):
    match = next((c for c in CATEGORIES if c["name"].lower() == slug.lower()), None)
    if not match:
        return jsonify({"error": "Category not found."}), 404
    start = request.args.get("start", 1, type=int)
    try:
        return jsonify(universal_search(match["name"], start))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

@app.post("/api/wishlist")
@login_required
def wishlist_api():
    payload = request.get_json(silent=True) or {}
    image_url = payload.get("image_url", "").strip()
    if not image_url:
        return jsonify({"error": "Image URL is required."}), 400
    try:
        image_url = validate_url(image_url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO wishlist(user_id,image_url,title,thumbnail_url) VALUES(?,?,?,?)", (session["user_id"], image_url, payload.get("title", ""), payload.get("thumbnail_url", image_url)))
        conn.commit()
    return jsonify({"ok": True})

@app.delete("/api/wishlist")
@login_required
def wishlist_delete():
    payload = request.get_json(silent=True) or {}
    image_url = payload.get("image_url", "")
    with get_db() as conn:
        conn.execute("DELETE FROM wishlist WHERE user_id=? AND image_url=?", (session["user_id"], image_url))
        conn.commit()
    return jsonify({"ok": True})

@app.post("/api/analyze")
def analyze():
    payload = request.get_json(silent=True) or {}
    page_url = payload.get("url", "")
    try:
        page_url = validate_url(page_url)
        response = requests.get(page_url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
        response.raise_for_status()
        validate_url(response.url)
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type:
            return jsonify({"error": "The URL does not appear to be an HTML page."}), 400
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                return jsonify({"error": "The webpage is too large to analyze."}), 413
            chunks.append(chunk)
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        if "user_id" in session:
            save_search(page_url, "url")
        raw_candidates = extract_image_candidates(response.url, html)
        results = []
        with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as executor:
            futures = [executor.submit(inspect_image, item, response.url) for item in raw_candidates]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
        results.sort(key=lambda x: (x["width"] * x["height"], x["bytes"]), reverse=True)
        return jsonify({"page_url": response.url, "count": len(results), "images": results})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not fetch the page: {exc}"}), 502
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Analyze failed")
        return jsonify({"error": f"Unexpected error: {exc}"}), 500


@app.post("/api/download")
def download():
    payload = request.get_json(silent=True) or {}
    urls = payload.get("urls", [])
    if not isinstance(urls, list) or not urls:
        return jsonify({"error": "Select at least one image."}), 400
    if len(urls) > MAX_IMAGES:
        return jsonify({"error": f"You can download at most {MAX_IMAGES} images at once."}), 400
    unique_urls, seen = [], set()
    for raw in urls:
        if not isinstance(raw, str):
            continue
        try:
            clean = validate_url(raw)
        except ValueError:
            continue
        if clean not in seen:
            seen.add(clean)
            unique_urls.append(clean)
    if not unique_urls:
        return jsonify({"error": "No valid image URLs were supplied."}), 400
    errors, downloaded = [], []
    with ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as executor:
        future_map = {executor.submit(fetch_bytes, url): url for url in unique_urls}
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                data, final_url, _ = future.result()
                downloaded.append((url, final_url, data))
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)})
    if not downloaded:
        return jsonify({"error": "None of the selected images could be downloaded."}), 502
    zip_buffer = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for index, (_, final_url, data) in enumerate(downloaded, 1):
            filename = filename_for(final_url, index, data)
            base, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
            candidate, counter = filename, 2
            while candidate in used_names:
                candidate = f"{base}_{counter}.{ext}" if ext else f"{base}_{counter}"
                counter += 1
            used_names.add(candidate)
            zf.writestr(candidate, data)
        if errors:
            zf.writestr("download_errors.json", json.dumps(errors, indent=2, ensure_ascii=False))
    if "user_id" in session:
        with get_db() as conn:
            conn.executemany("INSERT INTO download_history(user_id,image_url) VALUES(?,?)", [(session["user_id"], url) for url, _, _ in downloaded])
            conn.commit()
    zip_buffer.seek(0)
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True, download_name="image_downloader_hub.zip")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
