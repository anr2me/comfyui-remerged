"""
Low level HTTP transport and a profile-driven client.

Reference for the built-in provider: https://remerged.ai/developers
and https://remerged.ai/openapi.json

Design notes
------------
* Standard library only.  ``requests`` is used when it happens to be importable
  (ComfyUI usually ships it) because it is a little faster, but the node pack
  works without it.
* Everything service-specific - the paths, the request field names, the shape of
  the responses - comes from a *profile* (see :mod:`.profile`).  Swapping
  profiles is what makes this work against any OpenAPI 3.1 service.
* Errors follow the API's own contract: JSON bodies shaped like
  ``{"error": "<code>", "message": "<sentence>", "topup_url": "..."}``.  The
  HTTP status is authoritative, so it is always surfaced alongside the code.
* The presigned upload URL is signed for a very specific request, so uploads are
  sent with nothing but a ``Content-Type`` header attached.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import profile as profile_mod

DEFAULT_BASE_URL = "https://remerged.ai/api/v1"
DEFAULT_TIMEOUT = 120.0
USER_AGENT = "comfyui-remerged/1.0 (+https://remerged.ai/developers)"

#: Statuses that carry a human readable explanation we can improve on.
_STATUS_HINTS = {
    400: "The request was refused and nothing was charged.",
    401: "No key, or not a valid one. Check the API key (or the provider's env var).",
    402: "Not enough credits on the account.",
    403: "The key lacks the scope for this call, or the account is deactivated or frozen.",
    404: "No such job on this account.",
    429: "Over 60 requests a minute, or too many jobs already in flight.",
}


class RemergedError(RuntimeError):
    """Any non 2xx answer from the API."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        topup_url: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.topup_url = topup_url
        self.payload = payload or {}

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        prefix = []
        if self.code:
            prefix.append(f"[{self.code}]")
        if self.status:
            prefix.append(f"HTTP {self.status}")
        head = " ".join(prefix)
        return f"{head} {self.message}".strip()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"RemergedError(status={self.status}, code={self.code!r}, message={self.message!r})"


class RemergedTimeout(RuntimeError):
    """Raised when a job did not finish inside the requested time budget."""


def _requests():
    try:
        import requests  # type: ignore

        return requests
    except Exception:
        return None


def _looks_like_prose(text: str) -> bool:
    """The API sometimes puts a whole sentence in ``error`` instead of a code."""
    return " " in text.strip() or text.strip().endswith(".")


class ApiClient:
    """A small client that talks to any service a profile describes."""

    def __init__(
        self,
        profile: Dict[str, Any],
        api_key: str,
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.profile = profile or {}
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or self.profile.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._requests = _requests()

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return str(self.profile.get("name") or "provider")

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{str(path).lstrip('/')}"

    def _auth_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        auth = self.profile.get("auth") or {}
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        header = auth.get("header") or "Authorization"
        prefix = auth.get("prefix")
        if prefix is None:
            prefix = "Bearer " if str(auth.get("type") or "bearer").lower() == "bearer" else ""
        if self.api_key:
            headers[header] = f"{prefix}{self.api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _raw_request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Dict[str, str], bytes]:
        timeout = self.timeout if timeout is None else timeout

        if self._requests is not None:
            resp = self._requests.request(
                method,
                url,
                headers=headers or {},
                data=body,
                timeout=timeout,
            )
            return resp.status_code, dict(resp.headers), resp.content

        request = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:  # the API uses 4xx for real errors
            payload = exc.read()
            return exc.code, dict(exc.headers or {}), payload
        except urllib.error.URLError as exc:
            raise RemergedError(f"Could not reach {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RemergedError(f"Timed out after {timeout}s talking to {url}") from exc

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        expect: Tuple[int, ...] = (200, 201),
        timeout: Optional[float] = None,
    ) -> Any:
        url = self._url(path)
        headers = self._auth_headers({"Content-Type": "application/json"})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")

        status, _resp_headers, payload = self._raw_request(
            method, url, headers=headers, body=raw, timeout=timeout
        )

        parsed: Any = None
        if payload:
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except Exception:
                parsed = None

        if status in expect:
            return parsed if parsed is not None else {}

        raise self._build_error(status, parsed, payload)

    @staticmethod
    def _build_error(status: int, parsed: Any, raw: bytes) -> RemergedError:
        code: Optional[str] = None
        message: Optional[str] = None
        topup_url: Optional[str] = None

        if isinstance(parsed, dict):
            raw_error = parsed.get("error")
            raw_message = parsed.get("message")
            topup_url = parsed.get("topup_url")
            if isinstance(raw_error, str):
                if raw_message and not _looks_like_prose(raw_error):
                    code = raw_error
                    message = str(raw_message)
                elif _looks_like_prose(raw_error):
                    message = raw_error
                else:
                    code = raw_error
            if message is None and isinstance(raw_message, str):
                message = raw_message
            if isinstance(parsed.get("detail"), str) and message is None:
                message = parsed["detail"]
        elif isinstance(parsed, str) and parsed.strip():
            message = parsed.strip()

        if not message and raw:
            snippet = raw[:300].decode("utf-8", errors="replace").strip()
            if snippet:
                message = snippet

        hint = _STATUS_HINTS.get(status, "")
        if not message:
            message = hint or f"The API answered with status {status}."
        elif hint and hint not in message:
            message = f"{message} {hint}"

        return RemergedError(
            message.strip(),
            status=status,
            code=code,
            topup_url=topup_url,
            payload=parsed if isinstance(parsed, dict) else {},
        )

    # ------------------------------------------------------------------ #
    # profile plumbing
    # ------------------------------------------------------------------ #
    def _endpoint(self, key: str) -> Optional[Dict[str, Any]]:
        endpoint = (self.profile.get("endpoints") or {}).get(key)
        if not isinstance(endpoint, dict) or not endpoint.get("path"):
            return None
        return endpoint

    def _path(self, template: str, params: Optional[Dict[str, str]] = None) -> str:
        """Fill a path template, e.g. ``/jobs/{id}``."""
        params = params or {}

        def replace(match: "re.Match[str]") -> str:
            key = match.group(1)
            if key in params:
                return urllib.parse.quote(str(params[key]), safe="")
            return urllib.parse.quote(str(params.get("id", "")), safe="")

        return re.sub(r"\{([^}]+)\}", replace, str(template))

    def build_body(self, canonical: Dict[str, Any]) -> Dict[str, Any]:
        """Turn canonical fields into the service's own request body."""
        mapping = self.profile.get("job_fields") or {}
        body: Dict[str, Any] = {}
        for key, value in canonical.items():
            if value is None:
                continue
            target = mapping.get(key)
            if not target:
                continue
            body[target] = value
        return body

    def _extract(self, payload: Any, section: str, canonical: str, fallback_names: List[str], default: Any = None) -> Any:
        """Read a value out of a response using the profile's mapping."""
        mapping = (self.profile.get("responses") or {}).get(section) or {}
        path = mapping.get(canonical)
        if path:
            value = profile_mod.dig(payload, path)
            if value is not None:
                return value
        found = profile_mod.search_key(payload, fallback_names)
        return default if found is None else found

    # ------------------------------------------------------------------ #
    # endpoints
    # ------------------------------------------------------------------ #
    def get_models(self) -> Any:
        endpoint = self._endpoint("models")
        if not endpoint:
            raise RemergedError("This provider has no models endpoint.")
        return self._request(str(endpoint.get("method") or "GET").upper(), endpoint["path"])

    def quote(self, body: Dict[str, Any]) -> Any:
        endpoint = self._endpoint("quote")
        if not endpoint:
            raise RemergedError("This provider has no quote endpoint.")
        return self._request(str(endpoint.get("method") or "POST").upper(), endpoint["path"], body)

    def create_job(self, body: Dict[str, Any]) -> Dict[str, Any]:
        endpoint = self._endpoint("create")
        if not endpoint:
            raise RemergedError("This provider has no create endpoint.")
        payload = self._request(
            str(endpoint.get("method") or "POST").upper(), endpoint["path"], body, expect=(200, 201, 202)
        )

        job_id = self._extract(payload, "create", "id", profile_mod.CREATE_SYNONYMS["id"])
        job_ids = self._extract(payload, "create", "ids", profile_mod.CREATE_SYNONYMS["ids"])
        credits = self._extract(payload, "create", "credits", profile_mod.CREATE_SYNONYMS["credits"], 0)

        if isinstance(job_ids, str):
            job_ids = [job_ids]
        elif not isinstance(job_ids, list):
            job_ids = []
        if not job_ids and job_id:
            job_ids = [str(job_id)]

        return {
            "id": str(job_id) if job_id is not None else None,
            "ids": [str(item) for item in job_ids],
            "credits": int(credits or 0),
            "raw": payload,
        }

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """A job's status, and once ready a video URL."""
        endpoint = self._endpoint("status")
        if not endpoint:
            raise RemergedError("This provider has no status endpoint.")
        payload = self._request(
            str(endpoint.get("method") or "GET").upper(),
            self._path(endpoint["path"], {"id": job_id}),
        )
        envelope = ((self.profile.get("responses") or {}).get("status") or {}).get("envelope") or ""
        job = profile_mod.dig(payload, envelope) if envelope else payload
        if not isinstance(job, dict):
            job = payload if isinstance(payload, dict) else {}
        return job

    def job_field(self, job: Dict[str, Any], canonical: str, default: Any = None) -> Any:
        """Read a canonical field out of a job object."""
        mapping = self.profile.get("job_fields_out") or {}
        path = mapping.get(canonical)
        if path:
            value = profile_mod.dig(job, path)
            if value is not None:
                return value
        found = profile_mod.search_key(job, profile_mod.JOB_SYNONYMS.get(canonical, []))
        return default if found is None else found

    def job_status(self, job: Dict[str, Any]) -> str:
        """A job's status, normalised to our buckets."""
        raw = self.job_field(job, "status")
        return profile_mod.canonical_status(self.profile, raw)

    def list_jobs(self, limit: int = 20) -> Any:
        endpoint = self._endpoint("list")
        if not endpoint:
            raise RemergedError("This provider has no list endpoint.")
        limit = max(1, min(100, int(limit)))
        separator = "&" if "?" in endpoint["path"] else "?"
        return self._request(
            str(endpoint.get("method") or "GET").upper(),
            f"{endpoint['path']}{separator}limit={limit}",
        )

    def finish_job(
        self,
        job_id: str,
        mode: Optional[str] = None,
        webhook_url: Optional[str] = None,
    ) -> Any:
        endpoint = self._endpoint("finish")
        if not endpoint:
            raise RemergedError("This provider has no finish endpoint.")
        body: Dict[str, Any] = {}
        mapping = self.profile.get("job_fields") or {}
        if mode and mapping.get("mode"):
            body[mapping["mode"]] = mode
        if webhook_url and mapping.get("webhook_url"):
            body[mapping["webhook_url"]] = webhook_url
        return self._request(
            str(endpoint.get("method") or "POST").upper(),
            self._path(endpoint["path"], {"id": job_id}),
            body,
            expect=(200, 201, 202),
        )

    def get_account(self) -> Any:
        endpoint = self._endpoint("account")
        if not endpoint:
            raise RemergedError("This provider has no account endpoint.")
        return self._request(str(endpoint.get("method") or "GET").upper(), endpoint["path"])

    def create_upload(self, content_type: str, n_bytes: int) -> Dict[str, Any]:
        """Ask for a presigned PUT to upload an input file."""
        endpoint = self._endpoint("upload")
        if not endpoint:
            raise RemergedError("This provider has no upload endpoint.")
        fields = self.profile.get("upload_fields") or {}
        body = {
            fields.get("content_type", "content_type"): content_type,
            fields.get("bytes", "bytes"): int(n_bytes),
        }
        return self._request(
            str(endpoint.get("method") or "POST").upper(), endpoint["path"], body, expect=(200, 201)
        )

    def upload_ticket(self, content_type: str, n_bytes: int) -> Tuple[str, str]:
        """The ``(key, url)`` to PUT an upload to."""
        ticket = self.create_upload(content_type, n_bytes)
        if not isinstance(ticket, dict):
            raise RemergedError("The upload ticket was not a JSON object.")
        key = self._extract(ticket, "upload", "key", profile_mod.UPLOAD_SYNONYMS["key"])
        url = self._extract(ticket, "upload", "url", profile_mod.UPLOAD_SYNONYMS["url"])
        if not key or not url:
            raise RemergedError(
                "The upload ticket did not include a key and a url.",
                payload=ticket,
            )
        return str(key), str(url)

    def put_upload(self, url: str, data: bytes, content_type: str) -> None:
        """PUT the bytes at a presigned URL.

        Deliberately minimal: the URL is signed, so we send nothing but the
        content type.
        """
        headers = {"Content-Type": content_type}
        status, _headers, payload = self._raw_request(
            "PUT", url, headers=headers, body=data, timeout=max(self.timeout, 300.0)
        )
        if status < 200 or status >= 300:
            raise self._build_error(status, None, payload)

    def download(self, url: str, dest_path: str) -> str:
        """Stream a remote file to disk and return the path."""
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(request, timeout=max(self.timeout, 300.0)) as response:
            with open(dest_path, "wb") as handle:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
        return dest_path

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def upload_bytes(self, data: bytes, content_type: str) -> str:
        """Upload a blob and return the ``key`` to reference it in a job."""
        key, url = self.upload_ticket(content_type, len(data))
        self.put_upload(url, data, content_type)
        return key


def wait_for_job(
    client: ApiClient,
    job_id: str,
    *,
    interval: float = 10.0,
    timeout: float = 1800.0,
    on_poll=None,
    should_abort=None,
) -> Dict[str, Any]:
    """Poll the status endpoint until the job is ready.

    Returns the finished job object.  Raises :class:`RemergedError` when the job
    failed and :class:`RemergedTimeout` when the budget ran out.
    """
    deadline = time.time() + timeout
    last_status = None

    while True:
        if should_abort is not None and should_abort():
            raise RemergedError("Cancelled while waiting for the job.")

        job = client.get_job(job_id)
        status = client.job_status(job)

        if on_poll is not None:
            on_poll(job, status)

        if status == "ready":
            return job

        if status == "failed":
            detail = client.job_field(job, "error") or "The render failed."
            failure = job.get("failure") or {}
            code = failure.get("code") if isinstance(failure, dict) else None
            retry = failure.get("retry") if isinstance(failure, dict) else None
            extra = []
            if code:
                extra.append(f"code={code}")
            if retry:
                extra.append(f"retry={retry}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            raise RemergedError(
                f"{detail}{suffix}",
                code=code if isinstance(code, str) else None,
                payload=job,
            )

        if time.time() >= deadline:
            raise RemergedTimeout(
                f"Job {job_id} was still '{last_status or status}' after "
                f"{int(timeout)}s. It may still finish; check the provider's library."
            )

        last_status = status
        time.sleep(max(1.0, float(interval)))
