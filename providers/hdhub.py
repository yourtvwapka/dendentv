"""HDHub4u provider plugin (v2 — WP REST API + multi-strategy fallbacks)."""

import base64
import codecs
import html
import json
import re
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as creq


class Provider:
    id = "hdhub"

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

    CATEGORY_PATHS = [
        "/category/hollywood-movies/",
        "/category/bollywood-movies/",
        "/category/category/web-series/",
        "/category/south-hindi-movies/",
    ]

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

        # Strategy 1: WP REST API (works on virtually all WordPress sites)
        results = self._wp_rest_search(q)
        if results:
            print(f"[{self.id}] WP REST: {len(results)} results for {q!r}", flush=True)
            return {"query": q, "results": results}

        # Strategy 2: /search.html?s=  (the theme's real search route)
        results = self._scrape_page(self.base + "search.html?" + urlencode({"s": q}))
        if results:
            print(f"[{self.id}] search.html: {len(results)} results", flush=True)
            return {"query": q, "results": results}

        # Strategy 3: /?s=  (classic WP)
        results = self._scrape_page(self.base + "?s=" + quote(q))
        if results:
            print(f"[{self.id}] ?s=: {len(results)} results", flush=True)
            return {"query": q, "results": results}

        # Strategy 4: filter category listing pages by keyword
        results = self._scan_categories(q)
        if results:
            print(f"[{self.id}] category scan: {len(results)} results", flush=True)
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
        except Exception as e:
            print(f"[{self.id}] scrape {url} → {e}", flush=True)
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

    def _scan_categories(self, q):
        q_lower = q.lower()
        results, seen = [], set()
        for path in self.CATEGORY_PATHS:
            try:
                r = self._get(urljoin(self.base, path))
                if r.status_code != 200:
                    continue
            except Exception:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href]"):
                href = (a.get("href") or "").strip()
                title = a.get_text(" ", strip=True)
                if q_lower not in title.lower():
                    continue
                if not self._accept(href, title):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                results.append({"title": title, "url": urljoin(self.base, href), "type": "movie"})
            if len(results) >= 20:
                break
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

        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(" ", strip=True).lower()
            if any(k in href.lower() or k in text
                   for k in ("hubcloud", "gadgetsweb", "hubcdn", "hubdrive", "hubstream")):
                links.append({
                    "url": urljoin(self.base, href),
                    "label": a.get_text(" ", strip=True) or "Download",
                })

        streams = []
        for link in links:
            try:
                final = self._resolve_hubcloud(link["url"])
                if final:
                    streams.append({
                        "url": final,
                        "quality": self._guess_quality(link["label"]),
                        "format": "MP4",
                        "label": link["label"],
                    })
            except Exception:
                continue

        print(f"[{self.id}] get_streams {page_url} → {len(streams)} streams", flush=True)
        return {"streams": streams, "page": page_url}

    @staticmethod
    def _decrypt_token(token):
        t = base64.b64decode(token).decode()
        t = base64.b64decode(t).decode()
        t = codecs.decode(t, "rot13")
        t = base64.b64decode(t).decode()
        return json.loads(t)

    def _resolve_hubcloud(self, gadgetsweb_url):
        r = self._get(gadgetsweb_url)
        r.raise_for_status()
        m = (
            re.search(r'id=["\']token["\'][^>]*value=["\']([^"\']+)', r.text)
            or re.search(r'name=["\']token["\'][^>]*value=["\']([^"\']+)', r.text)
            or re.search(r'token\s*=\s*["\']([A-Za-z0-9+/=]{40,})["\']', r.text)
        )
        if not m:
            return None
        payload = self._decrypt_token(m.group(1))
        next_url = payload.get("url") or payload.get("link")
        if not next_url:
            return None
        r2 = self._get(next_url)
        r2.raise_for_status()
        if "text/html" not in (r2.headers.get("content-type") or "").lower():
            return str(r2.url)
        soup = BeautifulSoup(r2.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and not href.startswith(next_url):
                if any(h in href for h in ("hubcdn", "pixeldrain", ".mkv", ".mp4")):
                    return href
        return str(r2.url)

    @staticmethod
    def _guess_quality(label):
        m = re.search(r"(\d{3,4})p", label or "", re.I)
        return m.group(1) if m else "Auto"
