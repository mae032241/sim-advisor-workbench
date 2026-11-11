"""Small status views for the UI; full index manifests stay in the full API.

Mapping and representation are per-object retrieval data, not progress/status.
Skip them before descending so rendering a status page never copies their large
contents. Keep mode readiness, counts, errors, generations and job progress.
"""
from __future__ import annotations


def status_summary(value):
    if isinstance(value, dict):
        return {key: status_summary(item) for key, item in value.items()
                if key not in {"mapping", "representation"}}
    if isinstance(value, list):
        return [status_summary(item) for item in value]
    return value
