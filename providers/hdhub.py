"""HDHub4u provider plugin (v9 — hubdrive ajax with detailed attempt logging)."""

import html
import json
import re
from urllib.parse import quote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests as creq

HUB_PATTERNS = [
    r"hubcloud\.\w+", r"gadgetsweb\.\w+", r"hubcdn\.\w+",
    r"hubdrive\.\w+", r"hubstream\.\w+", r"hubfiles\.\w+",
    r"gdflix\.\w+", r"gdtot\.\w+", r"filepress\.\w+",
    r"kolop\.\w+", r"workers\.dev",
    r"googleusercontent\.com", r"pixeldrain\.com",
]

FILE_PATTERNS = [
    r"\.mkv(\?|$)", r"\.mp4(\?|$)", r"\.m3u8(\?|$)",
    r"hubcdn\.\w+/", r"workers\.dev/",
    r"pixeldrain\.com/api/file", r"pixeldrain\.com/u/",
]

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

    # ---------- get_streams (v9) ----------
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

        debug["candidates"] = sorted(list(all_urls))[:15]

        streams, seen = [], set()
        for u in all_urls:
            if u in seen:
                continue
            seen.add(u)
            host = urlparse(u).netloc.lower()

            if host.endswith("hubdrive.pics") and "/file/" not in u.lower():
                debug["resolutions"].append({"input": u[:150], "attempt": "skipped (homepage)"})
                continue

            try:
                final, attempt = self._resolve_any(u)
                debug["resolutions"].append({
                    "input": u[:200], "attempt": attempt,
                    "result": final[:200] if final else None,
                })
                if final and final not in seen:
                    seen.add(final)
                    streams.append({"url": final, "quality": "Auto",
                                    "format": "MP4", "label": "HDHub"})
            except Exception as e:
                debug["resolutions"].append({"input": u[:200], "error": str(e)[:200]})

        return {"streams": streams, "page": page_url, "debug": debug}

    def _resolve_any(self, url):
        host = urlparse(url).netloc.lower()
        if "hubdrive" in host:
            return self._resolve_hubdrive(url)
        if "hubcloud" in host:
            return self._resolve_hubcloud(url)
        return self._resolve_generic(url)

    # ---------- HubDrive (v9) ----------
    def _resolve_hubdrive(self, url):
        m = re.search(r"/file/([^/?#]+)", url)
        if not m:
            return None, "hubdrive: no /file/ in URL"
        url_id = m.group(1)

        # Step 1: load page (establishes session cookies)
        try:
            page_resp = self._get(url)
            page_html = page_resp.text
        except Exception as e:
            return None, {"message": "page load failed", "error": str(e)}

        # Capture cookies after page load
        cookies_after = []
        try:
            for c in self.session.cookies.jar:
                cookies_after.append({"name": c.name, "domain": c.domain})
        except Exception:
            pass

        # Step 2: find down-id in page
        down_id = None
        m2 = re.search(r'id=["\']down-id["\'][^>]*>([^<]+)<', page_html)
        if m2:
            down_id = m2.group(1).strip()
        else:
            m3 = re.search(r'id=["\']down-id["\'][^>]*value=["\']([^"\']+)', page_html)
            if m3:
                down_id = m3.group(1)
        if not down_id:
            m4 = re.search(r'down-id["\'][^>]*data-id=["\']([^"\']+)', page_html)
            if m4:
                down_id = m4.group(1)

        # Step 3: try POSTs with all combinations
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        ajax_urls = [origin + "/ajax.php?ajax=download", origin + "/ajax.php"]
        ids_to_try = []
        if down_id:
            ids_to_try.append(("down-id", down_id))
        ids_to_try.append(("url-id", url_id))

        attempts = []
        for label, candidate_id in ids_to_try:
            for ajax_url in ajax_urls:
                for body_kind, body_data, content_type in [
                    ("form-dict", {"id": candidate_id}, "application/x-www-form-urlencoded; charset=UTF-8"),
                    ("form-str", f"id={candidate_id}", "application/x-www-form-urlencoded; charset=UTF-8"),
                    ("form-body", {"ajax": "download", "id": candidate_id}, "application/x-www-form-urlencoded; charset=UTF-8"),
                ]:
                    headers = {
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": url,
                        "Origin": origin,
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Content-Type": content_type,
                    }
                    try:
                        resp = self.session.post(
                            ajax_url, data=body_data, headers=headers,
                            timeout=30, allow_redirects=False,
                        )
                        body_text = resp.text or ""
                        resp_headers = {}
                        for h in ("Location", "Content-Type", "Set-Cookie", "Content-Length"):
                            if h in resp.headers:
                                resp_headers[h] = resp.headers[h][:120]

                        attempts.append({
                            "src": label,
                            "id": candidate_id[:25],
                            "url": ajax_url.split("//")[1][:40],
                            "body_kind": body_kind,
                            "status": resp.status_code,
                            "bytes": len(body_text),
                            "preview": body_text[:80] if body_text else "",
                            "resp_headers": resp_headers,
                        })

                        # Success paths
                        if resp.status_code in (200, 302):
                            # Redirect to file?
                            loc = resp.headers.get("Location") or ""
                            if loc and any(re.search(p, loc, re.I) for p in FILE_PATTERNS):
                                return loc, f"hubdrive: 302 → file ({label})"

                            # JSON body?
                            if body_text.strip():
                                try:
                                    j = resp.json()
                                    if str(j.get("code") or "") == "200":
                                        file_url = j.get("file") or j.get("url") or j.get("link")
                                        if file_url:
                                            return file_url, f"hubdrive: JSON OK ({label}, {body_kind})"
                                except Exception:
                                    pass

                                # Non-JSON body that looks like HTML with file link
                                direct = self._scan_for_direct(body_text)
                                if direct:
                                    return direct, f"hubdrive: body scan ({label}, {body_kind})"
                    except Exception as e:
                        attempts.append({
                            "src": label, "id": candidate_id[:25],
                            "body_kind": body_kind, "error": str(e)[:120],
                        })

        # Step 4: nothing worked — dump diagnostics
        return None, {
            "message": "hubdrive: all POST variants failed",
            "url_id": url_id,
            "down_id": down_id,
            "cookies_after_page": cookies_after,
            "attempts": attempts[:12],
        }

    # ---------- HubCloud ----------
    def _resolve_hubcloud(self, url):
        try:
            r = self._get(url)
        except Exception as e:
            return None, f"hubcloud GET error: {e}"
        if r.status_code >= 400:
            return None, f"hubcloud HTTP {r.status_code}"

        # Check if hubcloud page embeds a hubdrive link
        for m in re.finditer(r"https?://hubdrive\.\w+/file/[^\s\"'<>]+", r.text):
            try:
                final, attempt = self._resolve_hubdrive(m.group(0))
                if final:
                    return final, f"hubcloud → hubdrive ({attempt if isinstance(attempt, str) else 'OK'})"
            except Exception:
                pass

        # Try hubcloud's own ajax.php
        m = re.search(r"/drive/([^/?#]+)", url)
        if m:
            file_id = m.group(1)
            origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            try:
                resp = self.session.post(
                    origin + "/ajax.php?ajax=download",
                    data={"id": file_id},
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": url,
                        "Origin": origin,
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    },
                    timeout=30,
                )
                if resp.status_code == 200 and resp.text.strip():
                    try:
                        j = resp.json()
                        if str(j.get("code")) == "200":
                            return j.get("file") or j.get("url"), "hubcloud ajax OK"
                    except Exception:
                        pass
            except Exception:
                pass

        direct = self._scan_for_direct(r.text)
        if direct:
            return direct, "hubcloud: direct scan"
        return None, "hubcloud: no match"

    def _resolve_generic(self, url):
        try:
            r = self._get(url)
        except Exception as e:
            return None, f"generic GET error: {e}"
        ct = (r.headers.get("content-type") or "").lower()
        if any(k in ct for k in ("video/", "application/octet-stream",
                                 "application/zip", "video/x-matroska")):
            return str(r.url), f"generic: file ({ct})"
        direct = self._scan_for_direct(r.text)
        if direct:
            return direct, "generic: scan"
        return None, "generic: no match"

    def _scan_for_direct(self, text):
        if not text:
            return None
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(re.search(p, href, re.I) for p in FILE_PATTERNS):
                return href
        for script in soup.find_all("script"):
            body = script.string or script.get_text() or ""
            for m in re.finditer(r"https?://[^\s\"'<>()\\]+", body):
                u = m.group(0)
                if any(re.search(p, u, re.I) for p in FILE_PATTERNS):
                    return u.rstrip(".,;)\"'")
        return None
