"""Supabase Storage S3 mirror for BMS Watch.

Allows running on Render's free tier (ephemeral disk) while persisting SQLite/data
to a private Supabase Storage bucket (matching openGym and food-tracker).

Environment Variables:
  MIRROR_ENDPOINT: https://<project>.storage.supabase.co/storage/v1/s3
  MIRROR_BUCKET:   default
  MIRROR_PREFIX:   bms_watch/
  MIRROR_REGION:   ap-southeast-1 (or auto)
  MIRROR_KEY_ID:   Supabase Storage S3 Access Key ID
  MIRROR_SECRET:   Supabase Storage S3 Secret Key
"""
import datetime
import hashlib
import hmac
import logging
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("bms_watch.mirror")


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _quote(s: str) -> str:
    return urllib.parse.quote(str(s), safe="-_.~")


def sign_s3_v4(method: str, url: str, body: bytes, key_id: str, secret: str, region: str = "ap-southeast-1"):
    """Generate AWS Signature Version 4 headers for Supabase S3 requests."""
    parsed = urllib.parse.urlsplit(url)
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    body_hash = _sha256(body)
    headers = {
        "host": parsed.netloc,
        "x-amz-content-sha256": body_hash,
        "x-amz-date": amz_date,
    }

    # Canonical query string
    qs_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    canonical_qs = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(qs_pairs))

    # Canonical headers
    sorted_headers = sorted((k.lower(), v.strip()) for k, v in headers.items())
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted_headers)
    signed_headers = ";".join(k for k, _ in sorted_headers)

    canonical_request = "\n".join([
        method.upper(),
        parsed.path,
        canonical_qs,
        canonical_headers,
        signed_headers,
        body_hash,
    ])

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        algorithm,
        amz_date,
        credential_scope,
        _sha256(canonical_request.encode("utf-8")),
    ])

    k_date = _hmac_sha256(f"AWS4{secret}".encode("utf-8"), date_stamp.encode("utf-8"))
    k_region = _hmac_sha256(k_date, region.encode("utf-8"))
    k_service = _hmac_sha256(k_region, b"s3")
    k_signing = _hmac_sha256(k_service, b"aws4_request")

    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth_header = (
        f"{algorithm} Credential={key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers["Authorization"] = auth_header
    return headers


class SupabaseStorageMirror:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.endpoint = os.getenv("MIRROR_ENDPOINT", "").rstrip("/")
        self.bucket = os.getenv("MIRROR_BUCKET", "default")
        self.prefix = os.getenv("MIRROR_PREFIX", "bms_watch/").strip("/")
        if self.prefix:
            self.prefix += "/"
        self.region = os.getenv("MIRROR_REGION", "ap-southeast-1")
        self.key_id = os.getenv("MIRROR_KEY_ID", "")
        self.secret = os.getenv("MIRROR_SECRET", "")
        self.enabled = bool(self.endpoint and self.key_id and self.secret)
        self._last_hashes: dict[str, str] = {}
        self._stop_event = threading.Event()

    def _s3_request(self, method: str, path: str, body: bytes = b"", query: dict = None) -> tuple[int, bytes]:
        url = f"{self.endpoint}/{_quote(self.bucket)}"
        if path:
            url += f"/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        headers = sign_s3_v4(method, url, body, self.key_id, self.secret, self.region)
        req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def pull(self):
        """Download remote files from Supabase S3 bucket on startup."""
        if not self.enabled:
            log.info("Mirror disabled: running purely against local disk (%s)", self.data_dir)
            return
        log.info("Mirror: Pulling remote state from %s (bucket: %s, prefix: %s)", self.endpoint, self.bucket, self.prefix)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # S3 list objects
        status, body = self._s3_request("GET", "", query={"prefix": self.prefix})
        if status not in (200, 204):
            log.warning("Mirror: List failed (%d): %s", status, body[:200])
            return

        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(body)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
            contents = root.findall("s3:Contents", ns) or root.findall("Contents")
            for item in contents:
                key_el = item.find("s3:Key", ns) if item.find("s3:Key", ns) is not None else item.find("Key")
                if key_el is None or not key_el.text:
                    continue
                full_key = key_el.text
                if not full_key.startswith(self.prefix):
                    continue
                rel_path = full_key[len(self.prefix):]
                if not rel_path or rel_path.endswith("/"):
                    continue

                # Download file
                s, data = self._s3_request("GET", full_key)
                if s == 200:
                    target = self.data_dir / rel_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    self._last_hashes[rel_path] = _sha256(data)
                    log.info("Mirror pulled: %s (%d bytes)", rel_path, len(data))
        except Exception:
            log.exception("Mirror pull failed parsing response")

    def push(self):
        """Upload any locally modified files to Supabase S3 bucket."""
        if not self.enabled or not self.data_dir.exists():
            return

        for p in self.data_dir.rglob("*"):
            if not p.is_file() or p.name.endswith(".tmp") or p.name.startswith("."):
                continue
            rel = str(p.relative_to(self.data_dir)).replace("\\", "/")
            try:
                content = p.read_bytes()
                h = _sha256(content)
                if self._last_hashes.get(rel) == h:
                    continue  # Unchanged

                full_key = f"{self.prefix}{rel}"
                status, _ = self._s3_request("PUT", full_key, body=content)
                if status in (200, 201, 204):
                    self._last_hashes[rel] = h
                    log.info("Mirror pushed: %s (%d bytes)", rel, len(content))
                else:
                    log.warning("Mirror push failed for %s (status %d)", rel, status)
            except Exception:
                log.exception("Mirror push exception for %s", rel)

    def start_background_sync(self, interval_sec: int = 5):
        if not self.enabled:
            return

        def _loop():
            while not self._stop_event.is_set():
                try:
                    self.push()
                except Exception:
                    log.exception("Background mirror push failed")
                self._stop_event.wait(interval_sec)

        t = threading.Thread(target=_loop, daemon=True, name="supabase-mirror-sync")
        t.start()

        def _handle_exit(sig, frame):
            log.info("Shutdown signal %s: performing final sync to Supabase...", sig)
            self._stop_event.set()
            self.push()
            sys.exit(0)

        try:
            signal.signal(signal.SIGTERM, _handle_exit)
            signal.signal(signal.SIGINT, _handle_exit)
        except (ValueError, AttributeError):
            pass  # On Windows threads or non-main threads
