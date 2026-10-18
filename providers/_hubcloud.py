"""Shared HubCloud / Gamerxyt / GadgetsWeb / HubDrive / GDflix resolver.

Confirmed flow (from two working open-source implementations):

  hubcloud.<tld>/drive/<id>
      → <a id="download"> href = gamerxyt.com/hubcloud.php?host=hubcloud&id=...&token=...
      → gamerxyt page exposes mirrors:
           <a id="s3">          FSLv2   (append _1{minute})
           <a id="fsl">         FSL     (append 1{minute})
           var pxl / id=pxl-1   Pixel   (pixeldrain page → /api/file/<id>?download)
           bzzhr.co anchor      Buzz
           pixel.hubcloud.cx    10Gbps  → redirects to video-downloads.googleusercontent.com
"""

import base64
import datetime
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

CLOUD_HOST_PATTERNS = [
    r"hubcloud\.\w+",
    r"gadgetsweb\.\w+",
    r"hubdrive\.\w+",
    r"hubcdn\.\w+",
    r"hubstream\.\w+",
    r"gdflix\.\w+",
    r"driveleech\.\w+",
    r"filepress\.\w+",
    r"gdtot\.\w+",
    r"kolop\.\w+",
    r"pixeldrain\.\w+",
]

DIRECT_HOST_PATTERNS = [
    r"video-downloads\.googleusercontent\.com",
    r"gigabytes\.icu",
    r"airbender\.buzz",
    r"\.mkv(\?|$)",
    r"\.mp4(\?|$)",
]


def find_cloud_links(text):
    """Extract every cloud-host URL from arbitrary HTML or JS text."""
    urls = set()
    if not text:
        return urls
    for m in re.finditer(r"https?://[^\s\"'<>()\\]+", text):
        u = m.group(0).rstrip(".,;)\"'")
        if any(re.search(p, u, re.I) for p in CLOUD_HOST_PATTERNS):
            urls.add(u.replace("&amp;", "&"))
    return urls


def resolve_cloud_url(session, url, referer=None):
    """Resolve one cloud URL into a list of {url, quality, format, label}."""
    host = urlparse(url).netloc.lower()

    if "hubdrive" in host:
        return _resolve_hubdrive(session, url)
    if "hubcloud" in host and "/drive/" in url:
        return _resolve_hubcloud(session, url, referer)
    if "gadgetsweb" in host:
        return _resolve_gadgetsweb(session, url, referer)
    # gdflix, driveleech, filepress → one-hop follow
    return _follow_shortener(session, url, referer)


# ----------------------------------------------------------------- hubcloud
def _resolve_hubcloud(session, landing_url, referer=None):
    """hubcloud.*/drive/<id> → gamerxyt generator page → mirror links."""
    hdrs = {"Referer": referer} if referer else {}
    try:
        r = session.get(landing_url, headers=hdrs, timeout=45, allow_redirects=True)
    except Exception as e:
        print(f"[hubcloud] landing GET failed: {e}", flush=True)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    gen_url = None
    a = soup.find("a", id="download")
    if a and a.get("href"):
        gen_url = a["href"].strip()
    else:
        m = re.search(r"https://gamerxyt\.com/hubcloud\.php\S+", r.text)
        if m:
            gen_url = m.group(0).replace("'", "").replace('"', "").replace(";", "")

    if not gen_url:
        print(f"[hubcloud] no generator link on {landing_url[:80]}", flush=True)
        return []

    gen_url = gen_url.replace("&amp;", "&")
    print(f"[hubcloud] generator: {gen_url[:120]}", flush=True)

    try:
        r2 = session.get(gen_url, headers={"Referer": landing_url},
                         timeout=45, allow_redirects=True)
    except Exception as e:
        print(f"[hubcloud] generator GET failed: {e}", flush=True)
        return []

    soup2 = BeautifulSoup(r2.text, "html.parser")
    out = []
    minute = datetime.datetime.now().minute

    # FSLv2 (<a id="s3">) — timing suffix _1{minute}
    a = soup2.find("a", id="s3")
    if a and a.get("href"):
        out.append({"url": a["href"] + f"_1{minute}",
                    "quality": "Auto", "format": "MKV", "label": "FSLv2"})

    # FSL (<a id="fsl">) — timing suffix 1{minute}
    a = soup2.find("a", id="fsl")
    if a and a.get("href"):
        out.append({"url": a["href"] + f"1{minute}",
                    "quality": "Auto", "format": "MKV", "label": "FSL"})

    # PixelServer
    m = re.search(r'var pxl = "([^"]+)"', r2.text)
    pxl = m.group(1) if m else None
    if not pxl:
        a = soup2.find("a", id="pxl-1")
        if a and a.get("href"):
            pxl = a["href"]
    if pxl:
        pm = re.search(r"pixeldrain\.\w+/u/([A-Za-z0-9]+)", pxl)
        if pm:
            pxl = f"https://pixeldrain.com/api/file/{pm.group(1)}?download"
        out.append({"url": pxl, "quality": "Auto", "format": "", "label": "Pixel"})

    # Buzz
    a = soup2.find("a", href=re.compile(r"bzzhr\.co"))
    if a and a.get("href"):
        out.append({"url": a["href"], "quality": "Auto",
                    "format": "", "label": "Buzz"})

    # 10Gbps (pixel.hubcloud.cx → gamerxyt/dl.php → Google CDN)
    a = soup2.find("a", href=re.compile(r"pixel\.hubcloud\.cx"))
    if a and a.get("href"):
        try:
            head = session.head(a["href"], headers={"Referer": gen_url},
                                allow_redirects=True, timeout=30)
            final = head.url
            if final and final != a["href"]:
                out.insert(0, {"url": final, "quality": "Auto",
                               "format": "MP4", "label": "10Gbps/Google"})
            else:
                out.append({"url": a["href"], "quality": "Auto",
                            "format": "", "label": "10Gbps"})
        except Exception:
            out.append({"url": a["href"], "quality": "Auto",
                        "format": "", "label": "10Gbps"})

    print(f"[hubcloud] {len(out)} mirrors from {landing_url[:60]}", flush=True)
    return out


# --------------------------------------------------------------- gadgetsweb
def _resolve_gadgetsweb(session, url, referer=None):
    """gadgetsweb.xyz/?id=... → decrypt token → hubcloud drive URL."""
    try:
        session.get(url, timeout=30)
    except Exception:
        pass
    # /homelander/ sets the session cookie
    try:
        session.get("https://gadgetsweb.xyz/homelander/", timeout=30)
    except Exception:
        pass
    try:
        r = session.get(url, timeout=30)
    except Exception:
        return []

    m = re.search(r"s\('o','([^']+)'", r.text)
    if not m:
        return []
    token = m.group(1)

    try:
        s1 = base64.b64decode(token).decode("utf-8")
        s2 = base64.b64decode(s1).decode("utf-8")
        s3 = _rot13(s2)
        padded = s3 + "=" * ((4 - len(s3) % 4) % 4)
        s4 = base64.b64decode(padded).decode("utf-8")
        data = json.loads(s4)
        hub_b64 = data.get("o")
        if not hub_b64:
            return []
        next_url = base64.b64decode(hub_b64).decode("utf-8")
    except Exception as e:
        print(f"[gadgetsweb] decrypt failed: {e}", flush=True)
        return []

    print(f"[gadgetsweb] → {next_url[:100]}", flush=True)
    return _resolve_hubcloud(session, next_url, referer)


# ----------------------------------------------------------------- hubdrive
def _resolve_hubdrive(session, file_url):
    """hubdrive.pics/file/<id> → POST /ajax.php?ajax=download {id}."""
    m = re.search(r"/file/([^/?#]+)", file_url)
    if not m:
        return []
    fid = m.group(1)
    origin = f"{urlparse(file_url).scheme}://{urlparse(file_url).netloc}"

    try:
        session.get(file_url, timeout=30)
    except Exception:
        pass

    ids = [fid]
    try:
        page = session.get(file_url, timeout=30)
        dm = re.search(r'id=["\']down-id["\'][^>]*>([^<]+)<', page.text)
        if dm and dm.group(1).strip():
            ids.insert(0, dm.group(1).strip())
    except Exception:
        pass

    for cid in ids:
        try:
            resp = session.post(
                origin + "/ajax.php?ajax=download",
                data={"id": cid},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": file_url,
                    "Origin": origin,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                timeout=30, allow_redirects=False,
            )
            txt = (resp.text or "").strip()
            if txt:
                j = json.loads(txt)
                if str(j.get("code")) == "200":
                    u = j.get("file") or j.get("url") or j.get("link")
                    if u:
                        return [{"url": u, "quality": "Auto",
                                 "format": "MP4", "label": "HubDrive"}]
            loc = resp.headers.get("Location")
            if loc and loc.startswith("http"):
                return [{"url": loc, "quality": "Auto",
                         "format": "MP4", "label": "HubDrive"}]
        except Exception:
            continue

    print(f"[hubdrive] ajax exhausted for {fid}", flush=True)
    return []


# ------------------------------------------------------------------ generic
def _follow_shortener(session, url, referer=None):
    hdrs = {"Referer": referer} if referer else {}
    try:
        r = session.get(url, headers=hdrs, timeout=45, allow_redirects=True)
    except Exception:
        return []

    ct = (r.headers.get("Content-Type") or "").lower()
    if any(k in ct for k in ("video/", "application/octet-stream")):
        return [{"url": str(r.url), "quality": "Auto",
                 "format": "MP4", "label": "Direct"}]

    out = []
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(re.search(p, href, re.I) for p in DIRECT_HOST_PATTERNS):
            out.append({"url": href, "quality": "Auto",
                        "format": "MP4", "label": "Direct"})
    return out


def _rot13(s):
    out = []
    for ch in s:
        if "a" <= ch <= "z":
            out.append(chr((ord(ch) - ord("a") + 13) % 26 + ord("a")))
        elif "A" <= ch <= "Z":
            out.append(chr((ord(ch) - ord("A") + 13) % 26 + ord("A")))
        else:
            out.append(ch)
    return "".join(out)
