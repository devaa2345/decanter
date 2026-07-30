"""
One-time setup script: push the current catalog_data.json into Supabase as
the first published catalog version, so the dashboard's version history and
the catalog-upload diff view have something real to start from instead of
an empty table.

Run once, after completing the steps in SUPABASE_SETUP.md (API keys, owner
login, storage bucket, schema migration, .env populated):

    python scripts/seed_catalog.py

Safe to re-run: it always adds a new version (source_filename
"catalog_data.json (seed)") and publishes it as active — running it twice
just adds a second identical version to the history, harmless.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.catalog import CATALOG_PATH  # noqa: E402
from app.db import CATALOG_BUCKET, SupabaseUnavailable, require_client  # noqa: E402


def main() -> None:
    try:
        client = require_client()
    except SupabaseUnavailable:
        print(
            "Supabase isn't configured. Fill in SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in your .env first — see SUPABASE_SETUP.md."
        )
        sys.exit(1)

    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    print(f"Seeding {len(catalog)} perfumes from {CATALOG_PATH.name} into Supabase...")

    insert_resp = (
        client.table("catalog_versions")
        .insert(
            {
                "status": "pending",
                "source_filename": "catalog_data.json (seed)",
                "storage_path": "",
                "perfume_count": len(catalog),
                "added_count": len(catalog),
                "updated_count": 0,
                "removed_count": 0,
                "diff": {"note": "initial seed from the bundled catalog_data.json"},
                "parse_warnings": [],
            }
        )
        .execute()
    )
    version_id = insert_resp.data[0]["id"]
    storage_path = f"v{version_id}.json"

    blob = json.dumps(catalog, ensure_ascii=False, indent=2).encode("utf-8")
    client.storage.from_(CATALOG_BUCKET).upload(
        storage_path, blob, {"content-type": "application/json", "upsert": "true"}
    )
    client.table("catalog_versions").update({"storage_path": storage_path}).eq("id", version_id).execute()

    from app.catalog_upload import publish_version

    published = publish_version(version_id)

    print(
        f"Done — version {version_id} is now the active published catalog "
        f"({published['perfume_count']} perfumes)."
    )
    print("Open /dashboard and log in to see it under Catalog versions.")


if __name__ == "__main__":
    main()
