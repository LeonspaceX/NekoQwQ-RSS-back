# server.py
"""
NekoQwQ RSS Reader - server.py
符合用户提供的后端思路文档要求。
- FastAPI 提供 API
- SQLite 存储数据（data.db 或 config.DB_FILE）
- feedparser 拉取 RSS
- cachetools 做 TTL 缓存
- 返回 XML 格式
- 只允许 GET 请求（非 GET -> 405）
"""

import sqlite3
import hashlib
import uuid
import threading
import time
import base64  # 新增导入base64模块
from datetime import datetime
import html
import os
from typing import Optional

import feedparser
from cachetools import TTLCache
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import config

# 缓存初始化
cache = TTLCache(maxsize=512, ttl=config.CACHE_TTL)

app = FastAPI()

# 添加CORS中间件，允许跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源的请求，生产环境中应该限制为特定域名
    allow_credentials=True,
    allow_methods=["GET"],  # 只允许GET请求
    allow_headers=["*"],
)

# ---------------- DB 初始化 ----------------
def get_conn():
    return sqlite3.connect(config.DB_FILE, check_same_thread=False)

def init_db():
    db_exists = os.path.exists(config.DB_FILE)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS articles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        publish_time TEXT NOT NULL,
        summary TEXT,
        source_id INTEGER,
        original_link TEXT NOT NULL,
        content TEXT,
        unique_id TEXT NOT NULL UNIQUE,
        last_seen_at INTEGER NOT NULL DEFAULT 0
    );
    """)
    # 索引加速查询
    cur.execute("CREATE INDEX IF NOT EXISTS idx_unique ON articles(unique_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source_id);")
    conn.commit()
    conn.close()
    if not db_exists:
        print(f"[init_db] Created new DB: {config.DB_FILE}")
    else:
        print(f"[init_db] Using existing DB: {config.DB_FILE}")

# ---------------- 工具函数 ----------------
def make_unique_id(original_link: str, publish_time: str) -> str:
    # 使用 md5(original_link + publish_time) 生成稳定的唯一 id，再转为Base64缩短长度
    key = (original_link or "") + (publish_time or "")
    hash_bytes = hashlib.md5(key.encode("utf-8")).digest()
    # 转为URL安全的Base64编码并移除填充字符'='
    return base64.urlsafe_b64encode(hash_bytes).decode().rstrip('=')

def safe_str(s: Optional[str]) -> str:
    return s if s and str(s).strip() else ""

def parse_publish_time(entry) -> str:
    """
    从 feedparser 的 entry 中提取发布时间，返回 ISO 格式字符串（YYYY-MM-DD 或完整时间）。
    如果找不到，返回文档指定的 "2099-01-01"
    """
    # 尝试几个常见字段
    for field in ("published", "updated", "pubDate", "created"):
        v = entry.get(field)
        if v:
            try:
                # feedparser 可能提供 parsed time
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    dt = datetime(*entry.published_parsed[:6])
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                return str(v)
            except Exception:
                return str(v)
    # fallback
    return "2099-01-01"

def pick_summary_and_content(entry):
    """
    从 entry 中提取摘要和内容。
    - 摘要(summary)会去除HTML标签，并截断为70个字符。
    - 内容(content)会保留完整原文。
    - 会过滤掉跟踪图片。
    """
    import re

    desc = safe_str(entry.get("summary") or entry.get("description") or "")
    cont = ""
    try:
        if entry.get("content"):
            c = entry.get("content")
            if isinstance(c, list) and len(c) > 0:
                cont = safe_str(c[0].get("value", ""))
            elif isinstance(c, dict):
                cont = safe_str(c.get("value", ""))
    except Exception:
        cont = ""

    def filter_tracking_img(text):
        pattern = r'^<img\s+[^>]*(?:height=[\"\']?1[\"\']?)[^>]*(?:width=[\"\']?1[\"\']?)[^>]*>|^<img\s+[^>]*(?:width=[\"\']?1[\"\']?)[^>]*(?:height=[\"\']?1[\"\']?)[^>]*>'
        return re.sub(pattern, '', text.strip())

    desc = filter_tracking_img(desc)
    cont = filter_tracking_img(cont)

    # 优先使用description，否则使用content作为摘要来源
    summary_source = desc or cont

    # 移除HTML标签以获得纯文本
    plain_text_summary = re.sub('<[^<]+?>', '', summary_source).strip()

    # 生成摘要，截断到70个字符
    if len(plain_text_summary) > 70:
        summary = plain_text_summary[:67] + "..."
    else:
        summary = plain_text_summary

    # 内容优先使用content，否则使用description
    content = cont or desc

    return summary, content

def xml_escape(s: str) -> str:
    # minimal xml escaping
    return html.escape(s or "")

def articles_count() -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM articles;")
    total = cur.fetchone()[0] or 0
    conn.close()
    return total

# ---------------- RSS 抓取与同步 ----------------
_fetch_lock = threading.Lock()

def fetch_all_feeds():
    """
    抓取 config.rss_links 中的每个 feed，进行插入/更新/删除同步。
    - 使用 unique_id（original_link + publish_time 的 md5）作为唯一标识
    - 如果数据库中已有相同 unique_id，但某些字段与当前抓取不一致 -> 更新
    - 如果数据库中存在来自此 source_id 的记录，但在本次抓取中没有出现 -> 视为被删除 -> 删除
    - 对每条记录更新 last_seen_at 为当前时间戳（用于判断是否被删除）
    """
    if not _fetch_lock.acquire(blocking=False):
        # already running
        print("[fetch_all_feeds] fetch already in progress, skipping")
        return
    try:
        print("[fetch_all_feeds] start fetch")
        now_ts = int(time.time())
        conn = get_conn()
        cur = conn.cursor()

        for src_idx, feed_url in enumerate(config.rss_links):
            print(f"[fetch_all_feeds] fetching {feed_url} (source_id={src_idx})")
            try:
                feed = feedparser.parse(feed_url)
            except Exception as e:
                print(f"[fetch_all_feeds] failed to parse {feed_url}: {e}")
                continue

            seen_ids = set()

            entries = getattr(feed, "entries", []) or []
            for entry in entries:
                title = safe_str(entry.get("title") or "")
                publish_time = parse_publish_time(entry) or "2099-01-01"
                summary, content = pick_summary_and_content(entry)
                original_link = safe_str(entry.get("link") or entry.get("id") or "")
                # If still no original_link, create a pseudo link using guid/title+time
                if not original_link:
                    original_link = "no-link-" + hashlib.md5((title + publish_time).encode()).hexdigest()

                # apply defaults if missing (only if that field missing)
                if not title:
                    title = "一篇来自rss订阅的文章"
                if not publish_time or publish_time.strip() == "":
                    publish_time = "2099-01-01"
                # source default: if source mapping missing, we still store index; but when returning, map to name or "RSS订阅"
                source_id = src_idx if src_idx < len(config.link_names) else None

                unique_id = make_unique_id(original_link, publish_time)
                seen_ids.add(unique_id)

                # Check if exists
                cur.execute("SELECT id, title, publish_time, summary, source_id, original_link, content FROM articles WHERE unique_id = ?",
                            (unique_id,))
                row = cur.fetchone()
                if row:
                    # exists: check if fields changed; if yes, update
                    db_title, db_publish_time, db_summary, db_source_id, db_original_link, db_content = row[1], row[2], row[3], row[4], row[5], row[6]
                    needs_update = False
                    if db_title != title or db_publish_time != publish_time or db_summary != summary or db_content != content or db_original_link != original_link or db_source_id != source_id:
                        needs_update = True
                    if needs_update:
                        cur.execute("""
                            UPDATE articles
                            SET title=?, publish_time=?, summary=?, source_id=?, original_link=?, content=?, last_seen_at=?
                            WHERE unique_id=?
                        """, (title, publish_time, summary, source_id, original_link, content, now_ts, unique_id))
                        print(f"[fetch_all_feeds] updated article {unique_id}")
                    else:
                        cur.execute("UPDATE articles SET last_seen_at=? WHERE unique_id=?", (now_ts, unique_id))
                else:
                    # insert
                    cur.execute("""
                        INSERT INTO articles (title, publish_time, summary, source_id, original_link, content, unique_id, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (title, publish_time, summary, source_id, original_link, content, unique_id, now_ts))
                    print(f"[fetch_all_feeds] inserted article {unique_id}")

            # 删除本源中已不存在的文章：即数据库中 source_id == src_idx 且 unique_id not in seen_ids
            cur.execute("SELECT unique_id FROM articles WHERE source_id = ?", (src_idx,))
            all_for_src = {r[0] for r in cur.fetchall()}
            to_delete = all_for_src - seen_ids
            for uid in to_delete:
                cur.execute("DELETE FROM articles WHERE unique_id = ?", (uid,))
                print(f"[fetch_all_feeds] deleted article {uid} (no longer in feed)")

        conn.commit()
        conn.close()
        # 清缓存，因为 DB 已变化
        cache.clear()
        print("[fetch_all_feeds] fetch complete")
    finally:
        _fetch_lock.release()

def start_periodic_fetch():
    def loop():
        # initial fetch done by caller; this thread waits then fetch repeatedly
        while True:
            time.sleep(config.FETCH_INTERVAL)
            try:
                fetch_all_feeds()
            except Exception as e:
                print(f"[start_periodic_fetch] error during fetch: {e}")

    t = threading.Thread(target=loop, daemon=True)
    t.start()

# ---------------- XML 输出生成 ----------------
def make_xml_root(tag: str, inner: str) -> str:
    return f"<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<{tag}>\n{inner}\n</{tag}>"

def article_to_xml_item_basic(row):
    # row: title, publish_time, summary, source_id, unique_id
    title = xml_escape(row[0])
    publish_time = xml_escape(row[1])
    summary = xml_escape(row[2] or "")
    source_idx = row[3]
    source_name = (config.link_names[source_idx] if source_idx is not None and source_idx < len(config.link_names) else "RSS订阅")
    source_name = xml_escape(source_name)
    unique_id = xml_escape(row[4])
    return f"""  <article>
    <title>{title}</title>
    <publish_time>{publish_time}</publish_time>
    <summary>{summary}</summary>
    <source>{source_name}</source>
    <unique_id>{unique_id}</unique_id>
  </article>"""

def article_to_xml_full(row):
    # row: title, publish_time, summary, source_id, original_link, content, unique_id
    title = xml_escape(row[0])
    publish_time = xml_escape(row[1])
    summary = xml_escape(row[2] or "")
    source_idx = row[3]
    source_name = (config.link_names[source_idx] if source_idx is not None and source_idx < len(config.link_names) else "RSS订阅")
    source_name = xml_escape(source_name)
    original_link = xml_escape(row[4] or "")
    content = xml_escape(row[5] or "")
    unique_id = xml_escape(row[6])
    return f"""  <article>
    <title>{title}</title>
    <publish_time>{publish_time}</publish_time>
    <summary>{summary}</summary>
    <source>{source_name}</source>
    <original_link>{original_link}</original_link>
    <content>{content}</content>
    <unique_id>{unique_id}</unique_id>
  </article>"""

# ---------------- 路由 ----------------

def require_get_or_405(request: Request):
    if request.method != "GET":
        # FastAPI will typically not call this route for other methods, but to follow doc strictly:
        raise HTTPException(status_code=405, detail="Method Not Allowed")

@app.get("/api/get/page-count")
async def api_page_count(request: Request):
    require_get_or_405(request)
    cache_key = "page_count"
    if cache_key in cache:
        pages = cache[cache_key]
    else:
        total = articles_count()
        pages = total // 9 + 1  # integer division, drop decimals
        cache[cache_key] = pages
    inner = f"  <page_count>{pages}</page_count>"
    xml = make_xml_root("response", inner)
    return Response(content=xml, media_type="application/xml")

@app.get("/api/get/basic_info/{page_id}")
async def api_basic_info(page_id: int, request: Request):
    require_get_or_405(request)
    if page_id < 1:
        raise HTTPException(status_code=400, detail="page_id must be >= 1")

    cache_key = f"basic_info_{page_id}"
    if cache_key in cache:
        xml = cache[cache_key]
        return Response(content=xml, media_type="application/xml")

    offset = (page_id - 1) * 9
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT title, publish_time, summary, source_id, unique_id
        FROM articles
        ORDER BY publish_time DESC
        LIMIT 9 OFFSET ?
    """, (offset,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        # Return empty list as xml but with page maybe empty
        inner = "  <articles />"
        xml = make_xml_root("response", inner)
        cache[cache_key] = xml
        return Response(content=xml, media_type="application/xml")

    items = [article_to_xml_item_basic(r) for r in rows]
    inner = "\n".join(items)
    xml = make_xml_root("response", inner)
    cache[cache_key] = xml
    return Response(content=xml, media_type="application/xml")

@app.get("/api/get/content/{unique_id}")
async def api_content(unique_id: str, request: Request):
    require_get_or_405(request)
    cache_key = f"content_{unique_id}"
    if cache_key in cache:
        xml = cache[cache_key]
        return Response(content=xml, media_type="application/xml")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT title, publish_time, summary, source_id, original_link, content, unique_id
        FROM articles WHERE unique_id = ?
    """, (unique_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    xml = make_xml_root("response", article_to_xml_full(row))
    cache[cache_key] = xml
    return Response(content=xml, media_type="application/xml")

@app.get("/api/get/info")
async def api_info(request: Request):
    require_get_or_405(request)
    info_str = f"NekoQwQ RSS Api v{config.API_VERSION} By Leonxie"
    xml = make_xml_root("response", f"  <info>{xml_escape(info_str)}</info>")
    return Response(content=xml, media_type="application/xml")

# catch-all for other routes: return 404 Not Found
@app.get("/{full_path:path}")
async def catch_all_get(full_path: str, request: Request):
    # for any GET on unknown path -> 404
    raise HTTPException(status_code=404, detail="Not Found")

# For any non-GET methods on any path, ensure 405 returned.
@app.api_route("/{full_path:path}", methods=["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def catch_all_non_get(full_path: str, request: Request):
    # 文档要求：客户端不使用 GET 则返回 405 Method Not Allowed
    raise HTTPException(status_code=405, detail="Method Not Allowed")

# ---------------- 启动逻辑 ----------------
def startup_tasks():
    init_db()
    # initial fetch
    try:
        fetch_all_feeds()
    except Exception as e:
        print(f"[startup_tasks] initial fetch failed: {e}")
    # start background periodic fetch thread
    start_periodic_fetch()

if __name__ == "__main__":
    startup_tasks()
    uvicorn.run("server:app", host=config.HOST, port=config.PORT, log_level="info")