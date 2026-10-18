"""HDHub4u provider plugin (v4 — hubdrive/hubcloud resolver with debug)."""

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
    r"googleusercontent\.com",
    r"pixeldrain\.com",
]

# Actual playable-file patterns (for scanning hubdrive/hubcloud landing pages)
FILE_PATTERNS = [
    r"\.mkv(\?|$)",
    r"\.mp4(\?|$)",
    r"\.m3u8(\?|$)",
    r"hubcdn\.\w+/",
    r"workers\.dev/",
    r"pixeldrain\.com/api/file",
    r"pixeldrain\.com/u/",
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

    # ---------- search (unchanged) ----------
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

    # ---------- get_streams (v4 with debug) ----------
    def get_streams(self, page_url):
        page_url = (page_url or "").strip()
        if not page_url:
            return {"streams": []}

        debug = {"candidates": [], "resolutions": []}

        try:
            r = self._get(page_url)
            r.raise_for_status()
        except Exception as e:
            return {"streams": [], "page": page_url, "debug": {"error": str(e)}}

        all_urls = set()

        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(True):
            for attr_val in tag.attrs.values():
                vals = attr_val if isinstance(attr_val, list) else [attr_val]
                for v in vals:
                    if isinstance(v, str) and "http" in v:
                        if any(re.search(p, v, re.I) for p in HUB_PATTERNS):
                            all_urls.add(v.strip())

        for script in soup.find_all("script"):
            body = script.string or script.get_text() or ""
            for m in re.finditer(r"https?://[^\s\"'<>()\\]+", body):
                u = m.group(0)
                if any(re.search(p, u, re.I) for p in HUB_PATTERNS):
                    all_urls.add(u.rstrip(".,;)\"'"))

        for m in re.finditer(r"https?://[^\s\"'<>()\\]+", r.text):
            u = m.group(0)
            if any(re.search(p, u, re.I) for p in HUB_PATTERNS):
                all_urls.add(u.rstrip(".,;)\"'"))

        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"].strip()
            if src.startswith(("http://", "https://")):
                if any(re.search(p, src, re.I) for p in HUB_PATTERNS):
                    all_urls.add(src)

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

        debug["candidates"] = sorted(list(all_urls))[:15]

        streams, seen = [], set()
        for u in all_urls:
            if u in seen:
                continue
            seen.add(u)
            try:
                final, attempt = self._resolve_hubcloud(u)
                debug["resolutions"].append({
                    "input": u[:200],
                    "attempt": attempt,
                    "result": final[:200] if final else None,
                })
                if final and final not in seen:
                    seen.add(final)
                    streams.append({
                        "url": final,
                        "quality": "Auto",
                        "format": "MP4",
                        "label": "HDHub",
                    })
            except Exception as e:
                debug["resolutions"].append({
                    "input": u[:200],
                    "error": str(e)[:200],
                })

        return {"streams": streams, "page": page_url, "debug": debug}

    @staticmethod
    def _decrypt_token(token):
        t = base64.b64decode(token).decode()
        t = base64.b64decode(t).decode()
        t = codecs.decode(t, "rot13")
        t = base64.b64decode(t).decode()
        return json.loads(t)

    def _resolve_hubcloud(self, url):
        """Return (final_url, attempt_description)."""
        try:
            r = self._get(url)
        except Exception as e:
            return None, f"GET error: {e}"

        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}"

        ct = (r.headers.get("content-type") or "").lower()
        clen = r.headers.get("Content-Length") or "0"

        # Already a file
        if any(k in ct for k in ("video/", "application/octet-stream", "application/zip", "application/x-matroska")):
            return str(r.url), f"direct file ({ct}, {clen} bytes)"

        html_text = r.text

        # 1. Token decrypt
        m = (
            re.search(r'id=["\']token["\'][^>]*value=["\']([^"\']+)', html_text)
            or re.search(r'name=["\']token["\'][^>]*value=["\']([^"\']+)', html_text)
            or re.search(r'token\s*=\s*["\']([A-Za-z0-9+/=]{40,})["\']', html_text)
        )
        if m:
            try:
                payload = self._decrypt_token(m.group(1))
                next_url = payload.get("url") or payload.get("link")
                if next_url:
                    r2 = self._get(next_url)
                    ct2 = (r2.headers.get("content-type") or "").lower()
                    if not any(k in ct2 for k in ("text/html", "text/plain")):
                        return str(r2.url), "token → non-html"
                    direct = self._scan_for_direct(r2.text)
                    if direct:
                        return direct, "token → follow → direct"
                    return str(r2.url), "token → follow (no direct found)"
            except Exception as e:
                pass

        # 2. Direct scan of page
        direct = self._scan_for_direct(html_text)
        if direct:
            return direct, "page scan found direct"

        # 3. POST to common endpoints
        for endpoint in ("/ajax/download", "/api/download", "/download", "/dl"):
            try:
                r2 = self.session.post(
                    urljoin(url, endpoint),
                    data={"id": url.rstrip("/").split("/")[-1]},
                    timeout=30, allow_redirects=True,
                )
                if r2.status_code == 200:
                    ct2 = (r2.headers.get("content-type") or "").lower()
                    if not any(k in ct2 for k in ("text/html", "text/plain")):
                        return str(r2.url), f"POST {endpoint} → file"
                    try:
                        data = r2.json()
                        for k in ("url", "link", "download", "file"):
                            if data.get(k):
                                return data[k], f"POST {endpoint} → JSON.{k}"
                    except Exception:
                        pass
                    direct = self._scan_for_direct(r2.text)
                    if direct:
                        return direct, f"POST {endpoint} → direct"
            except Exception:
                pass

        # 4. If the page itself is a file server (hubdrive sometimes serves directly)
        try:
            head = self.session.head(url, timeout=20, allow_redirects=True)
            ac = (head.headers.get("accept-ranges") or "").lower()
            ct3 = (head.headers.get("content-type") or "").lower()
            clen3 = int(head.headers.get("Content-Length") or "0")
            if ac == "bytes" or clen3 > 1_000_000:
                return str(head.url), f"accepts ranges ({ct3}, {clen3} bytes)"
        except Exception:
            pass

        return None, "no match"

    def _scan_for_direct(self, text):
        if not text:
            return None
        soup = BeautifulSoup(text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(re.search(p, href, re.I) for p in FILE_PATTERNS):
                return href

        for tag in soup.find_all(True):
            for attr_val in tag.attrs.values():
                vals = attr_val if isinstance(attr_val, list) else [attr_val]
                for v in vals:
                    if isinstance(v, str) and "http" in v:
                        if any(re.search(p, v, re.I) for p in FILE_PATTERNS):
                            return v

        for script in soup.find_all("script"):
            body = script.string or script.get_text() or ""
            for m in re.finditer(r"https?://[^\s\"'<>()\\]+", body):
                u = m.group(0)
                if any(re.search(p, u, re.I) for p in FILE_PATTERNS):
                    return u.rstrip(".,;)\"'")

        return None
