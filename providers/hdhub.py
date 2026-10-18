"""HDHub4u provider plugin.

Flow:
  search(q)        -> GET {base}/?s={query}, parse article anchors
  get_streams(url) -> scrape gadgetsweb/hubcloud links on the movie page,
                      decrypt each token, resolve the final direct-download URL
"""

import base64
import codecs
import json
import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as creq


class Provider:
    id = "hdhub"

    def __init__(self, info):
        self.info = info
        self.base = (info.get("url") or "").rstrip("/") + "/"
        self.session = creq.Session(impersonate="chrome120", verify=False)

    def _get(self, url, **kw):
        return self.session.get(url, timeout=45, allow_redirects=True, **kw)

    # ---------- search ----------
    def search(self, q):
        q = (q or "").strip()
        if not q:
            return {"results": []}
        url = self.base + "?s=" + quote(q)
        r = self._get(url)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        out, seen = [], set()
        for a in soup.select("article h2 a[href], h2.entry-title a[href]"):
            href = a.get("href", "").strip()
            title = a.get_text(" ", strip=True)
            if not href or not title or href in seen:
                continue
            seen.add(href)
            out.append({"title": title, "url": href, "type": "movie"})
        return {"query": q, "results": out}

    # ---------- page scrape ----------
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
                   for k in ("hubcloud", "gadgetsweb", "hubcdn", "hubdrive")):
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

        return {"streams": streams, "page": page_url}

    # ---------- HubCloud token decryption ----------
    @staticmethod
    def _decrypt_token(token):
        # token -> b64 -> b64 -> rot13 -> b64 -> JSON
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
