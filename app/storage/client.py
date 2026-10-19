"""Supabase Storage over its REST API: one private bucket, content-addressed paths.

Paths are <subject_id>/<sha256>.<ext>, so writing the same path twice writes the
same bytes; uploads use upsert and are safe to retry.
"""

from functools import lru_cache
from typing import Protocol

import httpx

from app.config import get_settings


class StorageError(Exception):
    pass


class StorageNotFound(StorageError):
    pass


class Storage(Protocol):
    def put(self, path: str, data: bytes, content_type: str) -> None: ...
    def get(self, path: str) -> bytes: ...
    def delete(self, path: str) -> None: ...
    def delete_many(self, paths: list[str]) -> None: ...
    def list(self, prefix: str) -> list[str]: ...


def object_path(subject_id: object, sha256: str, ext: str) -> str:
    return f"{subject_id}/{sha256}.{ext}" if ext else f"{subject_id}/{sha256}"


def page_image_path(sha256: str, page_no: int) -> str:
    """Rendered slide pages, keyed by file content so identical files share them."""
    return f"pages/{sha256}/{page_no:04d}.png"


def converted_pdf_path(sha256: str) -> str:
    """PDF made from a PPTX by LibreOffice, keyed by the PPTX's content."""
    return f"converted/{sha256}.pdf"


class SupabaseStorage:
    def __init__(self, url: str, service_key: str, bucket: str, timeout: float = 120.0):
        self.bucket = bucket
        headers = {"apikey": service_key}
        # Legacy service_role keys are JWTs and also go in Authorization;
        # new-style sb_secret_ keys are sent as apikey only.
        if service_key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {service_key}"
        self._http = httpx.Client(
            base_url=f"{url.rstrip('/')}/storage/v1", headers=headers, timeout=timeout
        )

    def put(self, path: str, data: bytes, content_type: str) -> None:
        response = self._http.post(
            f"/object/{self.bucket}/{path}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        _raise_for(response, f"upload {path}")

    def get(self, path: str) -> bytes:
        response = self._http.get(f"/object/{self.bucket}/{path}")
        _raise_for(response, f"download {path}")
        return response.content

    def delete(self, path: str) -> None:
        self.delete_many([path])

    def delete_many(self, paths: list[str]) -> None:
        # Deleting missing objects succeeds with an empty list, so this is idempotent.
        for i in range(0, len(paths), 500):
            chunk = paths[i : i + 500]
            response = self._http.request(
                "DELETE", f"/object/{self.bucket}", json={"prefixes": chunk}
            )
            _raise_for(response, f"delete {len(chunk)} object(s)")

    def list(self, prefix: str) -> list[str]:
        """Full paths of the objects directly under `prefix/`."""
        folder = prefix.rstrip("/")
        names: list[str] = []
        offset = 0
        while True:
            response = self._http.post(
                f"/object/list/{self.bucket}",
                json={"prefix": folder, "limit": 1000, "offset": offset},
            )
            _raise_for(response, f"list {folder}")
            batch = [item["name"] for item in response.json() if item.get("id")]
            names += [f"{folder}/{name}" for name in batch]
            if len(response.json()) < 1000:
                return names
            offset += 1000

    def ensure_bucket(self, file_size_limit: int) -> bool:
        """Create the private bucket if it doesn't exist. Returns True if it was created."""
        if self._http.get(f"/bucket/{self.bucket}").status_code == 200:
            return False
        response = self._http.post(
            "/bucket",
            json={
                "id": self.bucket,
                "name": self.bucket,
                "public": False,
                "file_size_limit": file_size_limit,
            },
        )
        _raise_for(response, f"create bucket {self.bucket}")
        return True


def _raise_for(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    # Supabase error bodies are short JSON messages; they never echo the key.
    try:
        body = response.json()
        detail = body.get("message") or response.text
    except ValueError:
        body, detail = {}, response.text
    # A missing object comes back as HTTP 400 with "statusCode": "404" in the body.
    missing = response.status_code == 404 or str(body.get("statusCode")) == "404"
    error = StorageNotFound if missing else StorageError
    raise error(f"Storage could not {action}: {response.status_code} {detail[:200]}")


@lru_cache
def get_storage() -> SupabaseStorage:
    """FastAPI dependency; tests override it with an in-memory fake."""
    settings = get_settings()
    return SupabaseStorage(
        settings.supabase_url,
        settings.supabase_service_key.get_secret_value(),
        settings.storage_bucket,
    )
