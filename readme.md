# diskover-mcp

A real MCP server (Streamable HTTP transport) that queries your Diskover
Elasticsearch index directly — no diskover-web layer needed.

## What's in here

- `server.py` — the MCP server itself
- `requirements.txt` — Python deps (mcp SDK + elasticsearch client, pinned to <9.0 since Diskover targets ES 8.x)
- `Dockerfile` — builds the server image
- `docker-compose.snippet.yml` — service block to merge into your existing compose file

## Setup

1. Copy this whole `diskover-mcp/` folder next to your existing `docker-compose.yml`
   (the one that already runs Elasticsearch and Diskover).

2. Open `docker-compose.snippet.yml`, copy the `diskover-mcp:` service block,
   and paste it into your existing `docker-compose.yml` under `services:`.
   Do NOT replace your other services — just add this one alongside them.

3. Check the `ES_HOST` environment variable matches the actual service name
   of your Elasticsearch container in that same compose file. The common
   default is `elasticsearch` (see the LinuxServer.io diskover compose
   example), but if yours differs, change it.

4. Check `ES_INDEX` — by default this queries `diskover-*` (a wildcard
   matching all Diskover indices). If you only want one specific index,
   set it explicitly, e.g. `diskover-2026.06.21`.

5. Rebuild and start:
   ```bash
   docker compose up -d --build diskover-mcp
   ```

6. Check the logs to confirm it started and could reach Elasticsearch:
   ```bash
   docker compose logs -f diskover-mcp
   ```
   You should see a line like `Resolved Diskover field names: {...}` —
   that confirms it could read your index mapping. If that line shows
   fallback/default values and your searches come back empty, your
   index's actual field names differ from the defaults; check the log
   warning above it and adjust `FIELD_CANDIDATES` in `server.py` if needed.

## Important: the actual MCP endpoint URL

FastMCP's Streamable HTTP transport mounts at `/mcp` by default — so the
real reachable URL is:

```
http://YOUR-IP:8005/mcp
```

not bare `http://YOUR-IP:8005`. Use the full path including `/mcp` in your
Claude Desktop config:

```json
{
  "mcpServers": {
    "diskover-mcp": {
      "type": "http",
      "url": "http://YOUR-IP:8005/mcp"
    }
  }
}
```

No `tools` block needed or supported — the server advertises its own
tools (`search_files`, `file_metadata`, `directory_size`, `find_duplicates`,
`es_query`) when Claude connects.

## Run indexing on diskover
Index need to be created or added in a cronjob in the container /config/cronjobs


## Run indexing manually

```
docker exec -u abc -d diskover python3 /app/diskover/diskover.py -i diskover-my_index_name /scan/Books Will run an index in the background
docker exec -u abc -it diskover python3 /app/diskover/diskover.py -i diskover-my_index_name /scan/Books Will run an index in the foreground
```
on my infra
```
docker exec -u abc -d diskover python3 /app/diskover/diskover.py -i diskover-downloads /scan/Downloads
```

## Mount backupserverFS
```
sudo sshfs qnap@192.168.0.190:/nas01 /media/backupserver
```

## Sanity-checking before wiring up Claude Desktop

Test Elasticsearch is reachable from where you're running this, independent
of the MCP layer:
```bash
curl http://YOUR-IP:9200/_cat/indices/diskover-*?v
```

Once the container is up, a quick raw protocol check (no MCP client needed):
```bash
curl -X POST http://YOUR-IP:8005/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```
A JSON-RPC response back (not a connection error) means the server is up
and speaking MCP correctly.

## Notes on the tools

- `search_files` — Elasticsearch `query_string` syntax against the whole index, optional path prefix filter.
- `file_metadata` — exact path lookup (splits into filename + parent dir internally).
- `directory_size` — sum + count aggregation under a path prefix.
- `find_duplicates` — terms aggregation grouping by hash field, min_doc_count 2.
- `es_query` — raw Query DSL passthrough for anything the above don't cover.

Field names (`filename` vs `name`, `path_parent` vs `parent_path`, etc.) are
auto-detected from your live index mapping at first use, since they can
differ slightly between Diskover versions/plugins. See `FIELD_CANDIDATES`
in `server.py` if you need to add more aliases for a custom schema.
