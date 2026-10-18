"""Create the private Supabase Storage bucket if it doesn't exist. Safe to re-run.

uv run python -m scripts.setup_storage
"""

from app.config import get_settings
from app.storage.client import get_storage


def main() -> None:
    settings = get_settings()
    created = get_storage().ensure_bucket(file_size_limit=settings.max_upload_bytes)
    state = "created" if created else "already exists"
    print(f"Bucket '{settings.storage_bucket}' {state}.")


if __name__ == "__main__":
    main()
