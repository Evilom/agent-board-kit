"""Portable Agent Board project-scoped retrieval over existing source files/indexes."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .common import NetworkError, now, request_json


def source_file(source, relative):
    # URI decoding, traversal and symlinks must all remain within the allowed root.
    relative = unquote(relative)
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(p in ("..", ".") for p in parts.parts) or "\\" in relative:
        return None
    prefixes = source.get("prefixes", [])
    if not any(relative == p.rstrip("/") or relative.startswith(p.rstrip("/") + "/")
               or p == "." for p in prefixes):
        return None
    root = Path(source["root"]).resolve()
    path = (root / relative).resolve()
    try:
        resolved_relative = path.relative_to(root).as_posix()
    except ValueError:
        return None
    if not any(resolved_relative == p.rstrip("/") or resolved_relative.startswith(p.rstrip("/") + "/")
               or p == "." for p in prefixes):
        return None
    if path.suffix.lower() not in (".md", ".txt", ".rst") or not path.is_file():
        return None
    return path


def search(sources, project_id, query, limit):
    results, availability = [], []
    terms = re.findall(r"\w+", query.casefold())
    if not terms:
        raise NetworkError("query must contain searchable text")
    for source_id, source in sources.items():
        if project_id not in source.get("projects", []):
            continue
        try:
            candidates = []
            if source["kind"] == "qmd-http":
                data = request_json(source, "/search", {"query": query, "n": 20})
                if data.get("ok") is not True or not isinstance(data.get("results"), list):
                    raise NetworkError("knowledge index returned no valid result set", 502)
                for item in data["results"]:
                    uri = urlsplit(item.get("file", ""))
                    if uri.scheme == "qmd" and uri.netloc == source["collection"]:
                        candidates.append((uri.path.lstrip("/"), item.get("docid")))
            elif source["kind"] == "documents":
                root = Path(source["root"])
                if not root.is_dir():
                    raise NetworkError("knowledge source unavailable", 502)
                # Only enumerate approved subtrees. No whole-disk or other-project index.
                for prefix in source["prefixes"]:
                    base = root / prefix
                    paths = [base] if base.is_file() else base.rglob("*")
                    for path in paths:
                        if path.is_file():
                            candidates.append((path.relative_to(root).as_posix(), None))
                        if len(candidates) > 5000:
                            raise NetworkError("source exceeds direct-search limit; use its existing index", 502)
            else:
                raise NetworkError("unsupported knowledge provider", 502)
            seen = set()
            for relative, index_version in candidates:
                path = source_file(source, relative)
                if path is None or path in seen:
                    continue
                seen.add(path)
                before = path.stat()
                if before.st_size > 2 * 1024 * 1024:
                    continue
                raw = path.read_bytes()
                after = path.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    continue
                content = raw.decode("utf-8", "replace")
                folded = content.casefold()
                if not all(term in folded for term in terms):
                    continue  # Drop stale index hits; never return old/deleted snippets.
                lines = content.splitlines()
                line = next((i for i, value in enumerate(lines) if any(t in value.casefold() for t in terms)), 0)
                title = next((v.lstrip("# ") for v in lines if v.startswith("# ")), path.name)
                results.append({"source_id": source_id, "project_id": project_id,
                                "uri": "%s://%s/%s" % ("qmd" if source["kind"] == "qmd-http" else "board-doc", source.get("collection", source_id), unquote(relative)),
                                "title": title[:200], "line": line + 1, "snippet": "\n".join(lines[line:line+12])[:2400],
                                "version": "sha256:" + hashlib.sha256(raw).hexdigest(),
                                "index_version": index_version, "source_mtime_ns": after.st_mtime_ns,
                                "verified_at": now(), "freshness": "source-read-at-query-time",
                                "fact_status": "source-document-not-implementation-proof"})
            availability.append({"source_id": source_id, "status": "available"})
        except (OSError, NetworkError) as exc:
            availability.append({"source_id": source_id, "status": "unavailable",
                                 "error": str(exc) if isinstance(exc, NetworkError) else "source read failed"})
    return {"project_id": project_id, "results": results[:limit], "sources": availability,
            "cached": False, "queried_at": now()}
