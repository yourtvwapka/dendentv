"""HDHub4u provider plugin (v3 — exhaustive HubCloud link extraction)."""

import base64
import codecs
import html
import json
import re
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

HUB_PATTERNS = [
    r"hubcloud\.\w+",
    r"gadgetsweb\.\w+",
    r"hubcdn\.\w+",
    r"hubdrive\.\w+",
    r"hubstream\.\w+",
    r"hubfiles\.\w+",
    r"gdflix\.\w+",
    r"gdtot\.\w+",
    r"filepress\.\w+",
    r"kolop\.\w+",
    r"workers\.dev",
    r"ampproject\.org/v/s/",
    r"googleusercontent\.com",
    r"pixeldrain\.com",
]

INTERMEDIATE_PATH_HINTS = (
    "/download", "/go/", "/redirect", "/file/", "/watch",
    "/dl/", "/link/", "/open/", "/get/", "/upload", "/r/",
)

SEARCH_SELECTORS = [
    "article h2 a[href]", "article h3 a[href]", "article h4 a[href]",
    "h2.entry-title a[href]", "h3.entry-title a[href]",
    "h2.post-title a[href]", "h3.post-title a[href]",
    ".post-title a[href]", ".entry-title a[href]",
    ".movie-title a[href]", ".title a[href]",
    ".item h2 a[href]", ".item h3 a[href]",
    "ul.recent-movies li a[href]",
]

REJECT_URL_HINTS = (
    "/category/", "/tag/", "/author/", "/feed/", "/page/",
    "/wp-content/", "/wp-admin/", "/wp-json/", "/xmlrpc",
    "?s=", "/#", "#comment", "javascript:", "mailto:",
    "/app/", "/dmca", "/privacy", "/contact", "/about",
    "/how-to-", "/telegram", "/disclaimer", "/request-a-movie",
    "/join-our-group", "/slider/", "/search.html",
)


class Provider:
    id = "hdhub"

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
        results = self._scrape_page(self.base + "search.html?" + urlencode({"s": q}))
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

    # ---------- get_streams (v3) ----------
    def get_streams(self, page_url):
        page_url = (page_url or "").strip()
        if not page_url:
            return {"streams": []}

        try:
            r = self._get(page_url)
            r.raise_for_status()
        except Exception as e:
            print(f"[{self.id}] page error: {e}", flush=True)
            return {"streams": [], "page": page_url}

        all_urls = set()

        # 1. all attributes
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(True):
            for attr_val in tag.attrs.values():
                vals = attr_val if isinstance(attr_val, list) else [attr_val]
                for v in vals:
                    if isinstance(v, str) and "http" in v:
                        if any(re.search(p, v, re.I) for p in HUB_PATTERNS):
                            all_urls.add(v.strip())

        # 2. script bodies
        for script in soup.find_all("script"):
            body = script.string or script.get_text() or ""
            for m in re.finditer(r"https?://[^\s\"'<>()\\]+", body):
                u = m.group(0)
                if any(re.search(p, u, re.I) for p in HUB_PATTERNS):
                    all_urls.add(u.rstrip(".,;)\"'"))

        # 3. raw HTML
        for m in re.finditer(r"https?://[^\s\"'<>()\\]+", r.text):
            u = m.group(0)
            if any(re.search(p, u, re.I) for p in HUB_PATTERNS):
                all_urls.add(u.rstrip(".,;)\"'"))

        # 4. iframes
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"].strip()
            if src.startswith(("http://", "https://")):
                if any(re.search(p, src, re.I) for p in HUB_PATTERNS):
                    all_urls.add(src)

        # 5. follow intermediate links (upload/xxx, go/xxx, r/xxx, etc.)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith(("http://", "https://")):
                href = urljoin(self.base, href)
            path = urlparse(href).path.lower()
            if any(hint in path for hint in INTERMEDIATE_PATH_HINTS):
                try:
                    r2 = self._get(href)
                    for m in re.finditer(r"https?://[^\s\"'<>()\\]+", r2.text):
                        u = m.group(0)
                        if any(re.search(p, u, re.I) for p in HUB_PATTERNS):
                            all_urls.add(u.rstrip(".,;)\"'"))
                except Exception:
                    continue

        print(f"[{self.id}] found {len(all_urls)} candidates", flush=True)
        for u in list(all_urls)[:5]:
            print(f"[{self.id}]   {u[:150]}", flush=True)

        # 6. resolve each hubcloud/gadgetsweb chain
        streams, seen = [], set()
        for u in all_urls:
            if u in seen:
                continue
            seen.add(u)
            try:
                final = self._resolve_hubcloud(u)
                if final and final not in seen:
                    seen.add(final)
                    streams.append({
                        "url": final,
                        "quality": "Auto",
                        "format": "MP4",
                        "label": "HDHub",
                    })
            except Exception as e:
                print(f"[{self.id}] resolve failed for {u[:80]}: {e}", flush=True)
                continue

        print(f"[{self.id}] get_streams → {len(streams)} streams", flush=True)
        return {"streams": streams, "page": page_url}

    @staticmethod
    def _decrypt_token(token):
        t = base64.b64decode(token).decode()
        t = base64.b64decode(t).decode()
        t = codecs.decode(t, "rot13")
        t = base64.b64decode(t).decode()
        return json.loads(t)

    def _resolve_hubcloud(self, url):
        r = self._get(url)
        if r.status_code >= 400:
            return None
        # If already a file, return as-is
        ct = (r.headers.get("content-type") or "").lower()
        if any(k in ct for k in ("video/", "application/octet-stream", "application/zip")):
            return str(r.url)

        # Look for token
        m = (
            re.search(r'id=["\']token["\'][^>]*value=["\']([^"\']+)', r.text)
            or re.search(r'name=["\']token["\'][^>]*value=["\']([^"\']+)', r.text)
            or re.search(r'token\s*=\s*["\']([A-Za-z0-9+/=]{40,})["\']', r.text)
        )
        if m:
            try:
                payload = self._decrypt_token(m.group(1))
                next_url = payload.get("url") or payload.get("link")
                if next_url:
                    r2 = self._get(next_url)
                    if "text/html" not in (r2.headers.get("content-type") or "").lower():
                        return str(r2.url)
                    soup = BeautifulSoup(r2.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if href.startswith("http") and any(
                            h in href for h in ("hubcdn", "pixeldrain", ".mkv", ".mp4", "hubcloud")
                        ):
                            return href
                    return str(r2.url)
            except Exception:
                pass

        # Fallback: look for direct link patterns on the hubcloud page
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(k in href.lower() for k in ("hubcdn", "pixeldrain", ".mkv", ".mp4", "workers.dev")):
                return href
        return None
