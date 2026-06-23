#!/usr/bin/env python3
"""
diskover_add_hash.py

Add a 'hash' field to Diskover Community Edition Elasticsearch documents
by reading each file from disk and computing a checksum.

Diskover CE does not calculate file hashes by default (that's normally
done via the separate diskover-dupesfinder tool, or in Enterprise).
This script fills that gap with a standalone Python implementation.

WHAT IT DOES:
    1. Ensures the index has a proper 'hash' mapping (keyword field).
    2. Scans the index for file docs that don't have a hash yet
       (so you can safely re-run this without re-hashing everything).
    3. Reads each file from disk and computes its checksum.
    4. Bulk-updates the ES documents with the hash value.

REQUIREMENTS:
    pip install elasticsearch --break-system-packages

USAGE:
    # Basic run - hash everything missing a hash field
    python diskover_add_hash.py --index diskover-myshare --host localhost

    # Use MD5 instead of SHA256 (faster, weaker - fine for dupe detection)
    python diskover_add_hash.py --index diskover-myshare --algo md5

    # Only hash files under a certain size to avoid huge files killing throughput
    python diskover_add_hash.py --index diskover-myshare --max-size-mb 500

    # Use multiple threads (hashing is I/O bound, threads help)
    python diskover_add_hash.py --index diskover-myshare --workers 8

    # Path translation if the script runs on a different host than diskover
    # scanned from (e.g. ES has /mnt/nfs/share/... but locally it's /data/...)
    python diskover_add_hash.py --index diskover-myshare \\
        --path-prefix-from /mnt/nfs/share --path-prefix-to /data

    # Dry run - show what would happen without writing to ES or reading files
    python diskover_add_hash.py --index diskover-myshare --dry-run
"""

import argparse
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan, bulk

CHUNK_SIZE = 8 * 1024 * 1024  # 8MB read chunks, good balance for large files


def ensure_hash_mapping(es: Elasticsearch, index: str, dry_run: bool):
    """Add a 'hash' keyword field to the mapping if it doesn't already exist."""
    mapping = es.indices.get_mapping(index=index)
    props = mapping[index]["mappings"].get("properties", {})
    if "hash" in props:
        print(f"Index '{index}' already has a 'hash' field mapped.", file=sys.stderr)
        return

    print(f"Adding 'hash' field mapping (keyword) to '{index}'...", file=sys.stderr)
    if dry_run:
        print("  [dry-run] skipped actual mapping update", file=sys.stderr)
        return

    es.indices.put_mapping(
        index=index,
        body={"properties": {"hash": {"type": "keyword"}}}
    )


def build_full_path(src: dict, prefix_from: str, prefix_to: str) -> str:
    path = src.get("parent_path", "")
    filename = src.get("name", "")
    full = os.path.join(path, filename)
    if prefix_from and full.startswith(prefix_from):
        full = prefix_to + full[len(prefix_from):]
    return full


def compute_hash(filepath: str, algo: str):
    h = hashlib.new(algo)
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest(), None
    except FileNotFoundError:
        return None, f"file not found: {filepath}"
    except PermissionError:
        return None, "permission denied"
    except OSError as e:
        return None, f"os error: {e}"


def fetch_docs_needing_hash(es: Elasticsearch, index: str, max_size_mb: int):
    """Yield (doc_id, source) for file docs that don't have a hash field yet."""
    must_not = [{"term": {"type": "directory"}}, {"exists": {"field": "hash"}}]
    query = {"query": {"bool": {"must_not": must_not}}}

    if max_size_mb:
        query["query"]["bool"]["filter"] = [
            {"range": {"filesize": {"lte": max_size_mb * 1024 * 1024}}}
        ]

    for hit in scan(es, index=index, query=query, _source=["parent_path", "name", "filesize"]):
        yield hit["_id"], hit["_source"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=9200)
    ap.add_argument("--scheme", default="http", choices=["http", "https"])
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--index", required=True)
    ap.add_argument("--algo", default="sha256", choices=["sha256", "md5", "sha1"],
                     help="hash algorithm (default: sha256; use md5 for speed)")
    ap.add_argument("--max-size-mb", type=int, default=None,
                     help="skip files larger than this size (MB) to control runtime")
    ap.add_argument("--workers", type=int, default=4,
                     help="number of parallel threads for hashing (I/O bound, default 4)")
    ap.add_argument("--bulk-batch-size", type=int, default=500,
                     help="how many ES updates to send per bulk request")
    ap.add_argument("--path-prefix-from", default=None,
                     help="path prefix as stored in ES (for translation)")
    ap.add_argument("--path-prefix-to", default=None,
                     help="path prefix to substitute when reading locally")
    ap.add_argument("--dry-run", action="store_true",
                     help="don't write to ES or hash files, just report counts")
    ap.add_argument("--limit", type=int, default=None,
                     help="only process the first N matching docs (testing)")
    args = ap.parse_args()

    es_kwargs = {"hosts": [f"{args.scheme}://{args.host}:{args.port}"]}
    if args.user and args.password:
        es_kwargs["basic_auth"] = (args.user, args.password)

    es = Elasticsearch(**es_kwargs)
    if not es.ping():
        print("ERROR: could not connect to Elasticsearch.", file=sys.stderr)
        sys.exit(1)

    ensure_hash_mapping(es, args.index, args.dry_run)

    print(f"Scanning '{args.index}' for file docs missing a hash...", file=sys.stderr)
    targets = list(fetch_docs_needing_hash(es, args.index, args.max_size_mb))
    if args.limit:
        targets = targets[:args.limit]

    total = len(targets)
    print(f"Found {total:,} files needing a hash.", file=sys.stderr)

    if args.dry_run:
        print("[dry-run] Not reading files or updating ES. Sample of targets:", file=sys.stderr)
        for doc_id, src in targets[:10]:
            fp = build_full_path(src, args.path_prefix_from, args.path_prefix_to)
            print(f"  {doc_id}  ->  {fp}")
        return

    if total == 0:
        print("Nothing to do.", file=sys.stderr)
        return

    actions_buffer = []
    done = 0
    errors = 0
    start = time.time()

    def process_one(item):
        doc_id, src = item
        fp = build_full_path(src, args.path_prefix_from, args.path_prefix_to)
        digest, err = compute_hash(fp, args.algo)
        return doc_id, fp, digest, err, src

    def flush_buffer():
        nonlocal actions_buffer
        if not actions_buffer:
            return
        success, errs = bulk(es, actions_buffer, raise_on_error=False, stats_only=False)
        if errs:
            print(f"  WARNING: {len(errs)} bulk update errors", file=sys.stderr)
        actions_buffer = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_one, item): item for item in targets}
        for future in as_completed(futures):
            doc_id, fp, digest, err, src = future.result()
            done += 1

            if err:
                errors += 1
                raw = f"parent_path={src.get('parent_path')!r} name={src.get('name')!r}"
                print(f"  [{done}/{total}] ERROR fp={fp!r} ({raw}): {err}", file=sys.stderr)
            else:
                print(f"  [{done}/{total}] OK    {fp}", file=sys.stderr)
                actions_buffer.append({
                    "_op_type": "update",
                    "_index": args.index,
                    "_id": doc_id,
                    "doc": {"hash": digest},
                })

            if len(actions_buffer) >= args.bulk_batch_size:
                flush_buffer()

            if done % 1000 == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                print(f"  progress: {done:,}/{total:,} ({rate:.1f} files/sec, {errors} errors)",
                      file=sys.stderr)

    flush_buffer()

    elapsed = time.time() - start
    print(f"\nDone. Hashed {done - errors:,} files, {errors} errors, in {elapsed:.1f}s.", file=sys.stderr)


if __name__ == "__main__":
    main()
