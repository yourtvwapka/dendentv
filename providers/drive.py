"""MoviesDrive provider plugin (v2 — WP REST API + multi-strategy fallbacks)."""

import html
import re
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

GDRIVE_ID_PATTERNS = [
    r"/file/d/([a-zA-Z0-9_-]+)",
    r"[?&]id=([a-zA-Z0-9_-]+)",
    r"/document/d/([a-zA-Z0-9_-]+)",
    r"/presentation/d/([a-zA-Z0-9_-]+)",
    r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
]


class Provider:
    id = "drive"

    SEARCH_SELECTORS = [
        "h2.entry-title a[href]", "h3.entry-title a[href]",
        "h2.post-title a[href]", "h3.post-title a[href]",
        ".post-title a[href]", ".entry-title a[href]",
        ".movie-title a[href]", ".title a[href]",
        "article h2 a[href]", "article h3 a[href]",
        ".item h2 a[href]", ".item h3 a[href]",
        ".card h2 a[href]", ".card h3 a[href]",
        ".movie-item a[href]", ".film-card a[href]",
        ".movie-card a[href]",
    ]

    REJECT_URL_HINTS = (
        "/category/", "/tag/", "/author/", "/feed/", "/page/",
        "/wp-content/", "/wp-admin/", "/wp-json/", "/xmlrpc",
        "?s=", "/#", "#comment", "javascript:", "mailto:",
        "/dmca", "/privacy", "/contact", "/about",
        "/telegram", "/disclaimer", "/how-to-",
    )

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

        # Strategy 1: WP REST API
        results = self._wp_rest_search(q)
        if results:
            print(f"[{self.id}] WP REST: {len(results)} results for {q!r}", flush=True)
            return {"query": q, "results": results}

        # Strategy 2: /?s=
        results = self._scrape_page(self.base + "?s=" + quote(q))
        if results:
            print(f"[{self.id}] ?s=: {len(results)} results", flush=True)
            return {"query": q, "results": results}

        print(f"[{self.id}] all strategies returned 0 for {q!r}", flush=True)
        return {"query": q, "results": []}

    def _wp_rest_search(self, q):
        try:
            url = self.base + "wp-json/wp/v2/search?" + urlencode({
                "search": q, "per_page": "30", "subtype": "post",
            })
            r = self._get(url)
            if r.status_code != 200:
                print(f"[{self.id}] wp-json HTTP {r.status_code}", flush=True)
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
            out = []
            for item in data:
                title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
                title = html.unescape(title)
                url = item.get("url") or ""
                if title and url and self._accept(url, title):
                    out.append({"title": title, "url": url, "type": "movie"})
            return out
        except Exception as e:
            print(f"[{self.id}] wp-json error: {e}", flush=True)
            return []

    def _scrape_page(self, url):
        try:
            r = self._get(url)
            r.raise_for_status()
        except Exception:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        results, seen = [], set()
        for sel in self.SEARCH_SELECTORS:
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
        if any(hint in low for hint in self.REJECT_URL_HINTS):
            return False
        if not low.startswith(("http://", "https://")):
            return True
        base_host = self.base.split("//", 1)[-1].split("/")[0]
        return base_host in low

    # ---------- get_streams ----------
    def get_streams(self, page_url):
        page_url = (page_url or "").strip()
        if not page_url:
            return {"streams": []}
        r = self._get(page_url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        raw_urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "drive.google.com" in href or "docs.google.com" in href:
                raw_urls.add(href)
        for tag in soup.find_all(attrs={"data-href": True}):
            v = tag["data-href"]
            if "drive.google.com" in v or "docs.google.com" in v:
                raw_urls.add(v)
        # Also grab drive links embedded inside inline JS
        for m in re.finditer(r"https?://drive\.google\.com/[^\s\"'<>]+", r.text):
            raw_urls.add(m.group(0))

        streams, seen = [], set()
        for u in raw_urls:
            fid, is_doc = self._extract_file_id(u)
            if not fid or fid in seen:
                continue
            seen.add(fid)
            streams.append({
                "url": self._direct_download_url(fid, is_doc),
                "quality": "Auto",
                "format": "MP4",
                "label": "Google Drive",
                "file_id": fid,
            })
        print(f"[{self.id}] get_streams {page_url} → {len(streams)} streams", flush=True)
        return {"streams": streams, "page": page_url}

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
