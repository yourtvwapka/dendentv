        # ── response headers ──
        upstream_len = r.headers.get("Content-Length")
        encoding = r.headers.get("Content-Encoding")
        compressed = encoding is not None and encoding.lower() not in ("", "identity")
        keep_alive = upstream_len is not None and not compressed

        # ═══ Cap concurrent media streams to prevent OOM ═══
        acquired = False
        if is_media and not head_only:
            if not STREAM_SEMAPHORE.acquire(blocking=False):
                try: r.close()
                except Exception: pass
                return self._send_body(503, json.dumps({
                    "error": "too many concurrent streams",
                    "hint": "Retry shortly",
                }), "application/json", head_only)
            acquired = True

        try:
            self.send_response(r.status_code)
            self._cors()

            cdn_ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if cdn_ct.startswith("video/") or cdn_ct == "application/octet-stream":
                out_ct = r.headers.get("Content-Type")
            else:
                out_ct = media_ct or r.headers.get("Content-Type")
            if out_ct: self.send_header("Content-Type", out_ct)

            for k in ("Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
                if k in r.headers: self.send_header(k, r.headers[k])
            if not is_api and "Cache-Control" in r.headers:
                self.send_header("Cache-Control", r.headers["Cache-Control"])
            elif is_api:
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
            if keep_alive:
                self.send_header("Content-Length", upstream_len)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()

            if head_only:
                return

            try:
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk: self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
            except Exception as e:
                print(f"[stream] write error: {e}", flush=True)
        finally:
            if acquired:
                STREAM_SEMAPHORE.release()
            try: r.close()
            except Exception: pass
            if is_media:
                gc.collect()
