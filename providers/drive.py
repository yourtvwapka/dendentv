"""MoviesDrive provider plugin (v4 — uses WP REST post content)."""

import html
import re
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

GDRIVE_ID_PATTERNS = [
    r"/file/d/([a-zA-Z0-9_-]+)",
    r"[?&]id=([a-zA-Z0-9_-]+)",
    r"/document/d/([a-zA-Z0-9_-]+)",
    r"/presentation/d/([a-zA-Z0-9_-]+)",
    r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
]

# Real file hosts (direct or 1-hop)
FILE_HOST_PATTERNS = [
    r"drive\.google\.com",
    r"docs\.google\.com",
    r"drive\.usercontent\.google\.com",
    r"driveleech\.\w+",
    r"gdflix\.\w+",
    r"filepress\.\w+",
    r"gdtot\.\w+",
    r"kolop\.\w+",
    r"filebee\.\w+",
    r"mega\.nz",
    r"dropbox\.com",
    r"hubdrive\.\w+",
    r"hubcloud\.\w+",
]

# Intermediate link hints (1-hop follow)
INTERMEDIATE_PATH_HINTS = (
    "/download", "/go/", "/redirect", "/file/", "/watch",
    "/dl/", "/link/", "/open/", "/get/", "/upload", "/r/",
)

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

    # ---------- search ----------
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

    # ---------- get_streams (v4) ----------
    def get_streams(self, page_url):
        page_url = (page_url or "").strip()
        if not page_url:
            return {"streams": []}

        # ---- 1. wp-json post content (clean rendered HTML of post body) ----
        rendered_html = self._fetch_post_content(page_url)
        if rendered_html:
            print(f"[{self.id}] wp-json post content: {len(rendered_html)} bytes", flush=True)
            urls = self._extract_file_urls(rendered_html, page_url)
            print(f"[{self.id}] extracted {len(urls)} candidate URLs from post content", flush=True)
            for u in list(urls)[:5]:
                print(f"[{self.id}]   {u[:150]}", flush=True)
            if urls:
                streams = self._build_streams(urls)
                print(f"[{self.id}] get_streams → {len(streams)} streams (wp-json)", flush=True)
                return {"streams": streams, "page": page_url}
        else:
            print(f"[{self.id}] wp-json post content: not available", flush=True)

        # ---- 2. fallback: rendered page ----
        try:
            r = self._get(page_url)
            r.raise_for_status()
        except Exception as e:
            print(f"[{self.id}] page fetch error: {e}", flush=True)
            return {"streams": [], "page": page_url}

        urls = self._extract_file_urls(r.text, page_url)
        print(f"[{self.id}] extracted {len(urls)} candidate URLs from page HTML", flush=True)
        for u in list(urls)[:5]:
            print(f"[{self.id}]   {u[:150]}", flush=True)

        streams = self._build_streams(urls)
        print(f"[{self.id}] get_streams → {len(streams)} streams (page HTML)", flush=True)
        return {"streams": streams, "page": page_url}

    def _fetch_post_content(self, page_url):
        """Return the 'content.rendered' HTML for the post, or '' on failure."""
        path = urlparse(page_url).path.strip("/")
        if not path:
            return ""
        slug = path.split("/")[-1]
        try:
            url = self.base + "wp-json/wp/v2/posts?" + urlencode({"slug": slug})
            r = self._get(url)
            if r.status_code != 200:
                print(f"[{self.id}] wp-json posts HTTP {r.status_code}", flush=True)
                return ""
            data = r.json()
            if not isinstance(data, list) or not data:
                return ""
            return data[0].get("content", {}).get("rendered", "") or ""
        except Exception as e:
            print(f"[{self.id}] wp-json posts error: {e}", flush=True)
            return ""

    def _extract_file_urls(self, text, page_url):
        """Pull every candidate URL out of arbitrary HTML/JS text."""
        urls = set()
        if not text:
            return urls

        # Direct URL literals
        for m in re.finditer(r"https?://[^\s\"'<>()\\]+", text):
            u = m.group(0).rstrip(".,;)\"'")
            if any(re.search(p, u, re.I) for p in FILE_HOST_PATTERNS):
                urls.add(u)

        # Attributes: data-url, data-href, data-link, onclick, etc.
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

        # 1-hop follow of intermediate links
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith(("http://", "https://")):
                href = urljoin(self.base, href)
            path = urlparse(href).path.lower()
            if any(hint in path for hint in INTERMEDIATE_PATH_HINTS):
                try:
                    r = self._get(href)
                    for m in re.finditer(r"https?://[^\s\"'<>()\\]+", r.text):
                        u = m.group(0).rstrip(".,;)\"'")
                        if any(re.search(p, u, re.I) for p in FILE_HOST_PATTERNS):
                            urls.add(u)
                except Exception:
                    continue

        return urls

    def _build_streams(self, urls):
        streams, seen = [], set()
        for u in urls:
            fid, is_doc = self._extract_file_id(u)
            if fid:
                if fid in seen:
                    continue
                seen.add(fid)
                streams.append({
                    "url": self._direct_download_url(fid, is_doc),
                    "quality": "Auto",
                    "format": "MP4",
                    "label": "Google Drive",
                    "file_id": fid,
                })
            else:
                if u in seen:
                    continue
                seen.add(u)
                # Skip search-recover endpoints — they're not files
                if "search-recover" in u:
                    continue
                streams.append({
                    "url": u,
                    "quality": "Auto",
                    "format": "",
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
        return (
            f"https://drive.usercontent.google.com/download"
            f"?id={file_id}&export=download&authuser=0"
        )
