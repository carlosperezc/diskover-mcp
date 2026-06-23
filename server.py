"""
diskover-mcp: an MCP server that queries a Diskover Elasticsearch index directly.

Transport: Streamable HTTP, served on port 8005 (matches the port you
originally configured). Talks to Elasticsearch over the ES Python client
using the standard Diskover index schema, with field-name auto-detection
so it tolerates minor schema differences between Diskover versions.

Tools exposed:
    - search_files       free-text / field search across the index
    - file_metadata       full document for a single exact path
    - directory_size      aggregate size + item count under a path prefix
    - find_duplicates     group files sharing the same hash
    - es_query             raw Elasticsearch Query DSL passthrough (power-user escape hatch)

Environment variables (see docker-compose.yml):
    ES_HOST      hostname of the Elasticsearch container (default: "elasticsearch")
    ES_PORT      Elasticsearch port (default: 9200)
    ES_INDEX     index name or pattern to query (default: "diskover-*")
    ES_USERNAME  optional, only needed if security is enabled
    ES_PASSWORD  optional, only needed if security is enabled
    ES_USE_SSL   "true"/"false" (default: "false")
    MCP_PORT     port this MCP server listens on (default: 8005)
"""

import os
import logging
from typing import Any

from elasticsearch import Elasticsearch
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("diskover-mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ES_HOST = os.environ.get("ES_HOST", "elasticsearch")
ES_PORT = int(os.environ.get("ES_PORT", "9200"))
ES_INDEX = os.environ.get("ES_INDEX", "diskover-*")
ES_USERNAME = os.environ.get("ES_USERNAME")
ES_PASSWORD = os.environ.get("ES_PASSWORD")
ES_USE_SSL = os.environ.get("ES_USE_SSL", "false").lower() == "true"
MCP_PORT = int(os.environ.get("MCP_PORT", "8005"))

scheme = "https" if ES_USE_SSL else "http"
es_kwargs: dict[str, Any] = {"hosts": [f"{scheme}://{ES_HOST}:{ES_PORT}"]}
if ES_USERNAME and ES_PASSWORD:
    es_kwargs["basic_auth"] = (ES_USERNAME, ES_PASSWORD)
if ES_USE_SSL:
    es_kwargs["verify_certs"] = os.environ.get("ES_VERIFY_CERTS", "true").lower() == "true"

es = Elasticsearch(**es_kwargs)

# Diskover's schema has shifted slightly across versions/plugins. Rather than
# hardcode one field name, we probe the actual index mapping once at startup
# and pick the first candidate that exists. Fallbacks below match the
# long-standing default Diskover schema.
FIELD_CANDIDATES = {
    "name": ["filename", "name"],
    "path_parent": ["path_parent", "parent_path"],
    "filesize": ["filesize", "size"],
    "filehash": ["filehash", "hash", "dupe_md5"],
    "is_dir": ["is_dir"],
    "mtime": ["mtime", "last_modified"],
    "owner": ["owner", "user"],
}

_resolved_fields: dict[str, str] | None = None


def resolve_fields() -> dict[str, str]:
    """Inspect the index mapping once and cache which field names this
    Diskover instance actually uses. Falls back to the first candidate
    if none are found in the mapping (query will then just 404 clearly)."""
    global _resolved_fields
    if _resolved_fields is not None:
        return _resolved_fields

    resolved: dict[str, str] = {}
    try:
        mapping = es.indices.get_mapping(index=ES_INDEX)
        all_props: set[str] = set()
        for _idx, body in mapping.items():
            props = body.get("mappings", {}).get("properties", {})
            all_props.update(props.keys())
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch index mapping (%s); using default field names", exc)
        all_props = set()

    for logical_name, candidates in FIELD_CANDIDATES.items():
        match = next((c for c in candidates if c in all_props), candidates[0])
        resolved[logical_name] = match

    _resolved_fields = resolved
    log.info("Resolved Diskover field names: %s", resolved)
    return resolved


mcp = FastMCP("diskover-mcp", host="0.0.0.0", port=MCP_PORT)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_files(query: str, path: str | None = None, max_results: int = 50) -> dict:
    """Search the Diskover index for files/directories matching a query string.

    Args:
        query: Elasticsearch query_string syntax, e.g. "extension:pdf AND size:>1048576"
               or just a plain filename fragment like "invoice".
        path: Optional path prefix to restrict results to (e.g. "/mnt/data/projects").
        max_results: Maximum number of hits to return (default 50, capped at 500).
    """
    fields = resolve_fields()
    max_results = min(max_results, 500)

    must: list[dict] = [{"query_string": {"query": query}}]
    if path:
        must.append({"prefix": {fields["path_parent"]: path}})

    body = {"query": {"bool": {"must": must}}, "size": max_results}

    result = es.search(index=ES_INDEX, body=body)
    hits = result.get("hits", {}).get("hits", [])
    return {
        "total": result.get("hits", {}).get("total", {}).get("value"),
        "results": [
            {"id": h["_id"], "index": h["_index"], **h.get("_source", {})}
            for h in hits
        ],
    }


@mcp.tool()
def file_metadata(path: str) -> dict:
    """Get full indexed metadata for a single file or directory by exact path.

    Args:
        path: Full path to the file or directory, e.g. "/mnt/data/report.pdf".
    """
    fields = resolve_fields()
    filename = path.rsplit("/", 1)[-1]
    parent = path.rsplit("/", 1)[0] or "/"

    body = {
        "query": {
            "bool": {
                "must": [
                    {"term": {f"{fields['name']}.keyword": filename}},
                    {"term": {f"{fields['path_parent']}.keyword": parent}},
                ]
            }
        },
        "size": 1,
    }

    result = es.search(index=ES_INDEX, body=body)
    hits = result.get("hits", {}).get("hits", [])
    if not hits:
        return {"found": False, "path": path}

    h = hits[0]
    return {"found": True, "id": h["_id"], "index": h["_index"], **h.get("_source", {})}


@mcp.tool()
def directory_size(path: str) -> dict:
    """Get total size and item count for everything under a directory path.

    Args:
        path: Directory path prefix, e.g. "/mnt/data/projects".
    """
    fields = resolve_fields()

    body = {
        "query": {"prefix": {fields["path_parent"]: path}},
        "size": 0,
        "aggs": {
            "total_size": {"sum": {"field": fields["filesize"]}},
            "file_count": {"value_count": {"field": fields["filesize"]}},
        },
    }

    result = es.search(index=ES_INDEX, body=body)
    aggs = result.get("aggregations", {})
    return {
        "path": path,
        "total_size_bytes": aggs.get("total_size", {}).get("value", 0),
        "file_count": aggs.get("file_count", {}).get("value", 0),
        "matched_docs": result.get("hits", {}).get("total", {}).get("value"),
    }


@mcp.tool()
def find_duplicates(path: str | None = None, min_size: int = 1, max_groups: int = 50) -> dict:
    """Find groups of files that share the same hash (duplicate content).

    Args:
        path: Optional path prefix to restrict the search to.
        min_size: Ignore files smaller than this many bytes (default 1, to skip empty files).
        max_groups: Maximum number of duplicate groups to return (default 50, capped at 200).
    """
    fields = resolve_fields()
    max_groups = min(max_groups, 200)

    must: list[dict] = [
        {"range": {fields["filesize"]: {"gte": min_size}}},
        {"bool": {"must_not": {"term": {fields["filehash"]: ""}}}},
    ]
    if path:
        must.append({"prefix": {fields["path_parent"]: path}})

    body = {
        "query": {"bool": {"must": must}},
        "size": 0,
        "aggs": {
            "by_hash": {
                "terms": {"field": fields["filehash"], "min_doc_count": 2, "size": max_groups},
                "aggs": {
                    "sample_files": {"top_hits": {"size": 10}},
                    "wasted_space": {
                        "bucket_script": {
                            "buckets_path": {"count": "_count"},
                            "script": "(params.count - 1)",
                        }
                    },
                },
            }
        },
    }

    result = es.search(index=ES_INDEX, body=body)
    buckets = result.get("aggregations", {}).get("by_hash", {}).get("buckets", [])

    groups = []
    for b in buckets:
        files = [
            hit.get("_source", {})
            for hit in b.get("sample_files", {}).get("hits", {}).get("hits", [])
        ]
        groups.append({"hash": b["key"], "count": b["doc_count"], "files": files})

    return {"duplicate_groups_found": len(groups), "groups": groups}


@mcp.tool()
def es_query(query: dict, size: int = 50) -> dict:
    """Run a raw Elasticsearch Query DSL query against the Diskover index.
    Use this for anything the other tools don't cover (custom aggregations,
    date ranges, complex bool queries, etc).

    Args:
        query: A valid Elasticsearch query object, e.g. {"match": {"filename": "report"}}.
        size: Number of hits to return (default 50, capped at 500). Set to 0 if you only
              want aggregations back.
    """
    size = min(size, 500)
    body = {"query": query, "size": size}
    result = es.search(index=ES_INDEX, body=body)
    return result.body if hasattr(result, "body") else dict(result)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Streamable HTTP transport so this is reachable as an "http" type
    # server in Claude Desktop's config. Note: the actual endpoint path
    # is /mcp by default, e.g. http://<host>:8005/mcp
    mcp.run(transport="streamable-http")
