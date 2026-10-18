"""MoviesDrive provider plugin.

Flow:
  search(q)        -> GET {base}/?s={query}, parse post anchors
  get_streams(url) -> extract drive.google.com / docs.google.com links,
                      convert to direct-download URLs
"""

import re
from urllib.parse import quote

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
        for a in soup.select("h2.entry-title a[href], article h2 a[href], .post-title a[href]"):
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

        raw_urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "drive.google.com" in href or "docs.google.com" in href:
                raw_urls.add(href)
        for tag in soup.find_all(attrs={"data-href": True}):
            v = tag["data-href"]
            if "drive.google.com" in v or "docs.google.com" in v:
                raw_urls.add(v)

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
        return {"streams": streams, "page": page_url}

    # ---------- Google Drive helpers ----------
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
