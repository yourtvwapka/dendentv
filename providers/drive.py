"""MoviesDrive provider plugin (v8 — tries post meta, then Playwright-free scan)."""

import html
import re
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

GDRIVE_ID_PATTERNS = [
    r"/file/d/([a-zA-Z0-9_-]+)",
    r"[?&]id=([a-zA-Z0-9_-]+)",
]

FILE_HOST_PATTERNS = [
    r"drive\.google\.com", r"docs\.google\.com",
    r"drive\.usercontent\.google\.com",
    r"driveleech\.\w+", r"gdflix\.\w+", r"filepress\.\w+",
    r"gdtot\.\w+", r"kolop\.\w+", r"filebee\.\w+",
    r"mega\.nz", r"dropbox\.com",
    r"hubdrive\.\w+", r"hubcloud\.\w+",
]

SEARCH_SELECTORS = [
    "h2.entry-title a[href]", "h3.entry-title a[href]",
    "h2.post-title a[href]", "h3.post-title a[href]",
    ".post-title a[href]", ".entry-title a[href]",
    ".movie-title a[href]", ".title a[href]",
    "article h2 a[href]", "article h3 a[href]",
]

REJECT_URL_HINTS = (
    "/category/", "/tag/", "/author/", "/feed/", "/page/",
    "/wp-content/", "/wp-admin/", "/wp-json/", "/xmlrpc",
    "?s=", "/#", "#comment", "javascript:", "mailto:",
    "/dmca", "/privacy", "/contact", "/about",
    "/telegram", "/disclaimer", "/how-to-",
)


class Provider:
    id = "drive"

    def __init__(self, info):
        self.info = info
        self.base = (info.get("url") or "").rstrip("/") + "/"
        self.session = creq.Session(impersonate="chrome131", verify=False)

    def _get(self, url, **kw):
        return self.session.get(url, timeout=45, allow_redirects=True, **kw)

    def search(self, q):
        q = (q or "").strip()
        if not q:
            return {"results": []}
        results = self._wp_rest_search(q)
        if results:
            return {"query": q, "results": results}
        results = self._scrape_page(self.base + "?s=" + quote(q))
        return {"query": q, "results": results}

    def _wp_rest_search(self, q):
        try:
            url = self.base + "wp-json/wp/v2/search?" + urlencode({
                "search": q, "per_page": "50", "subtype": "post",
            })
            r = self._get(url)
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
        except Exception:
            return []
        items = []
        for item in data:
            title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
            title = html.unescape(title)
            url = item.get("url") or ""
            if title and url and self._accept(url, title):
                items.append({"title": title, "url": url, "type": "movie"})
        q_lower = q.lower()
        q_words = [w for w in re.split(r"\W+", q_lower) if len(w) >= 3]
        title_matches = [
            it for it in items
            if q_lower in it["title"].lower()
            or all(w in it["title"].lower() for w in q_words)
        ]
        return title_matches or items

    def _scrape_page(self, url):
        try:
            r = self._get(url)
            r.raise_for_status()
        except Exception:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results, seen = [], set()
        for sel in SEARCH_SELECTORS:
            for a in soup.select(sel):
                href = (a.get("href") or "").strip()
                title = a.get_text(" ", strip=True)
                if not self._accept(href, title):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                results.append({"title": title, "url": urljoin(self.base, href), "type": "movie"})
            if results:
                return results
        return results

    def _accept(self, href, title):
        if not href or not title:
            return False
        if len(title) < 4 or len(title) > 200:
            return False
        low = href.lower()
        if any(hint in low for hint in REJECT_URL_HINTS):
            return False
        if not low.startswith(("http://", "https://")):
            return True
        base_host = self.base.split("//", 1)[-1].split("/")[0]
        return base_host in low

    # ---------- get_streams (v8) ----------
    def get_streams(self, page_url):
        page_url = (page_url or "").strip()
        if not page_url:
            return {"streams": []}

        debug = {"sources": {}, "all_urls": []}
        all_urls = set()

        path = urlparse(page_url).path.strip("/")
        slug = path.split("/")[-1] if path else ""
        post_id = None

        # Fetch full post object via wp-json (has meta, content, everything)
        if slug:
            try:
                r = self._get(self.base + "wp-json/wp/v2/posts?" + urlencode({"slug": slug}))
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        post = data[0]
                        post_id = post.get("id")
                        debug["sources"]["post_id"] = post_id

                        # Content
                        content = post.get("content", {}).get("rendered", "") or ""
                        content_urls = self._extract_file_urls(content)
                        all_urls |= content_urls
                        debug["sources"]["content_bytes"] = len(content)
                        debug["sources"]["content_urls"] = len(content_urls)

                        # Excerpt
                        excerpt = post.get("excerpt", {}).get("rendered", "") or ""
                        excerpt_urls = self._extract_file_urls(excerpt)
                        all_urls |= excerpt_urls
                        debug["sources"]["excerpt_bytes"] = len(excerpt)
                        debug["sources"]["excerpt_urls"] = len(excerpt_urls)

                        # Meta (depends on theme exposing it)
                        meta = post.get("meta") or {}
                        meta_urls = set()
                        if isinstance(meta, dict):
                            for k, v in meta.items():
                                if isinstance(v, str) and "http" in v:
                                    for m in re.finditer(r"https?://[^\s\"'<>()\\]+", v):
                                        u = m.group(0).rstrip(".,;)\"'")
                                        if any(re.search(p, u, re.I) for p in FILE_HOST_PATTERNS):
                                            meta_urls.add(u)
                                elif isinstance(v, list):
                                    for item in v:
                                        if isinstance(item, str) and "http" in item:
                                            for m in re.finditer(r"https?://[^\s\"'<>()\\]+", item):
                                                u = m.group(0).rstrip(".,;)\"'")
                                                if any(re.search(p, u, re.I) for p in FILE_HOST_PATTERNS):
                                                    meta_urls.add(u)
                        all_urls |= meta_urls
                        debug["sources"]["meta_keys"] = sorted(list(meta.keys())) if isinstance(meta, dict) else []
                        debug["sources"]["meta_urls"] = sorted(list(meta_urls))[:5]
            except Exception as e:
                debug["sources"]["wp_json_error"] = str(e)

        # Try single-post endpoint with ?context=edit (sometimes exposes meta)
        if post_id:
            try:
                r = self._get(self.base + f"wp-json/wp/v2/posts/{post_id}?context=edit")
                if r.status_code == 200:
                    data = r.json()
                    meta = data.get("meta") or {}
                    if isinstance(meta, dict):
                        meta_urls = set()
                        for k, v in meta.items():
                            if isinstance(v, str) and "http" in v:
                                meta_urls |= self._extract_file_urls(v)
                        all_urls |= meta_urls
                        debug["sources"]["edit_meta_urls"] = sorted(list(meta_urls))[:5]
                        debug["sources"]["edit_meta_keys"] = sorted(list(meta.keys()))
            except Exception as e:
                debug["sources"]["edit_error"] = str(e)

        # Try custom REST endpoints the theme may expose
        if post_id:
            for endpoint in (
                f"wp-json/moviesdrive/v1/downloads?post_id={post_id}",
                f"wp-json/moviesdrive/v2/downloads?id={post_id}",
                f"wp-json/movie/v1/links?post_id={post_id}",
                f"wp-json/wp/v2/download?post={post_id}",
            ):
                try:
                    r = self._get(self.base + endpoint)
                    if r.status_code == 200 and len(r.text) > 30:
                        urls = self._extract_file_urls(r.text)
                        if urls:
                            all_urls |= urls
                            debug["sources"].setdefault("custom_endpoints", []).append({
                                "endpoint": endpoint, "urls": len(urls),
                            })
                except Exception:
                    continue

        # Fallback: rendered page scan
        try:
            r = self._get(page_url)
            page_urls = self._extract_file_urls(r.text)
            all_urls |= page_urls
            debug["sources"]["page_bytes"] = len(r.text)
            debug["sources"]["page_urls"] = len(page_urls)
        except Exception as e:
            debug["sources"]["page_error"] = str(e)

        debug["all_urls"] = sorted(list(all_urls))[:20]

        streams = self._build_streams(all_urls)
        return {"streams": streams, "page": page_url, "debug": debug}

    def _extract_file_urls(self, text):
        urls = set()
        if not text:
            return urls
        for m in re.finditer(r"https?://[^\s\"'<>()\\]+", text):
            u = m.group(0).rstrip(".,;)\"'")
            if any(re.search(p, u, re.I) for p in FILE_HOST_PATTERNS):
                urls.add(u)
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup.find_all(True):
            for attr_val in tag.attrs.values():
                vals = attr_val if isinstance(attr_val, list) else [attr_val]
                for v in vals:
                    if isinstance(v, str):
                        for m in re.finditer(r"https?://[^\s\"'<>()\\]+", v):
                            u = m.group(0).rstrip(".,;)\"'")
                            if any(re.search(p, u, re.I) for p in FILE_HOST_PATTERNS):
                                urls.add(u)
        return urls

    def _build_streams(self, urls):
        streams, seen = [], set()
        for u in urls:
            if "search-recover" in u:
                continue
            fid, is_doc = self._extract_file_id(u)
            if fid:
                if fid in seen:
                    continue
                seen.add(fid)
                streams.append({
                    "url": self._direct_download_url(fid, is_doc),
                    "quality": "Auto", "format": "MP4",
                    "label": "Google Drive", "file_id": fid,
                })
            else:
                if u in seen:
                    continue
                seen.add(u)
                streams.append({
                    "url": u, "quality": "Auto", "format": "",
                    "label": "Direct",
                })
        return streams

    @staticmethod
    def _extract_file_id(url):
        for pat in GDRIVE_ID_PATTERNS:
            m = re.search(pat, url)
            if m:
                is_doc = any(x in url for x in
                             ("docs.google.com", "sheets.google.com", "slides.google.com"))
                return m.group(1), is_doc
        return None, False

    @staticmethod
    def _direct_download_url(file_id, is_doc=False):
        if is_doc:
            return f"https://docs.google.com/document/d/{file_id}/export?format=pdf"
        return (f"https://drive.usercontent.google.com/download"
                f"?id={file_id}&export=download&authuser=0")
