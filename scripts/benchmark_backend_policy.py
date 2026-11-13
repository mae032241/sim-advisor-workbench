"""Execution-only retrieval controls for a benchmark's existing product tools.

Instructions, schemas, scopes, reads and saves are unchanged. Fixed experiments
override only search_knowledge.mode at the common planning-service dispatcher.
The same provider call supplies backend evidence; no extra query is performed.
"""
from __future__ import annotations

import copy
from contextvars import ContextVar
import time

BACKENDS = ('agent', 'qmd_keyword')
_SEARCH_MODES = {'native', 'qmd_keyword', 'qmd_vector'}


class BackendPolicyError(RuntimeError):
    """The experiment did not execute its declared retrieval backend."""


def policy_description(backend):
    if backend not in BACKENDS:
        raise ValueError('retrieval backend must be agent or qmd_keyword; the independent native backend is retired')
    return {'selection': backend, 'fixed': backend != 'agent',
            'effective_search_mode': 'agent_requested' if backend == 'agent' else backend,
            'enforcement': 'benchmark-only PlanningService.call wrapper; same runtime/HTTP/MCP tools',
            'backend_evidence': 'same provider call raw retrieval.backend or legacy retrieval_backend; mismatch/missing evidence fails',
            'empty_results': 'allowed and explicitly recorded without hit-level backend evidence',
            'automatic_fallback': False, 'extra_queries': False,
            'instructions_changed': False, 'tool_schema_changed': False}


def install_backend_policy(service, backend, *, on_search=None):
    """Install before starting the server; default agent leaves it untouched."""
    description = policy_description(backend)
    if backend == 'agent':
        return description
    if getattr(service, '_benchmark_backend_policy_installed', False):
        raise ValueError('A retrieval backend policy is already installed')
    original_call, original_search = service.call, service.kb.search
    current = ContextVar('benchmark_retrieval_backend', default=None)

    def checked_search(query, limit=8, kind='all', filters=None):
        record = current.get()
        if record is None:
            return original_search(query, limit=limit, kind=kind, filters=filters)
        if not isinstance(filters, dict) or filters.get('mode') != backend:
            raise BackendPolicyError('Provider retrieval mode differs from the fixed experiment backend')
        record['provider_calls'] += 1
        hits = original_search(query, limit=limit, kind=kind, filters=filters)
        if not isinstance(hits, list):
            raise BackendPolicyError('Provider returned no inspectable retrieval result list')
        for hit in hits:
            retrieval = hit.get('retrieval') if isinstance(hit, dict) else None
            actual = retrieval.get('backend') if isinstance(retrieval, dict) else None
            if actual is None and isinstance(hit, dict):
                actual = hit.get('retrieval_backend')
            record['observed_backends'].append(actual)
            if actual != 'qmd_keyword':
                raise BackendPolicyError(f'Retrieval backend mismatch: expected {backend}, observed {actual!r}; no fallback allowed')
        return hits

    def controlled_call(sid, name, arguments, *, provenance=None):
        if name != 'search_knowledge':
            return original_call(sid, name, arguments, provenance=provenance)
        requested = arguments.get('mode', 'qmd_keyword') if isinstance(arguments, dict) else None
        record = {'tool': name, 'session_id': sid, 'requested_mode': requested,
                  'effective_mode': backend, 'policy': backend, 'provider_calls': 0,
                  'observed_backends': [], 'success': False}
        started = time.monotonic()
        token = current.set(record)
        try:
            if not isinstance(arguments, dict):
                raise ValueError('Tool arguments must be a JSON object')
            if not isinstance(requested, str) or requested not in _SEARCH_MODES:
                raise ValueError('Requested retrieval mode must be native, qmd_keyword or qmd_vector')
            effective = copy.deepcopy(arguments)
            effective['mode'] = backend
            record['arguments'] = copy.deepcopy(arguments)
            result = original_call(sid, name, effective, provenance=provenance)
            if not isinstance(result, dict) or result.get('mode') != backend or not isinstance(result.get('results'), list):
                raise BackendPolicyError('Tool did not return its fixed effective mode and result list')
            if record['provider_calls'] == 0:
                raise BackendPolicyError('Search did not reach the backend-verified provider')
            record['result_count'] = len(result['results'])
            record['backend_validation'] = 'verified_hits' if record['observed_backends'] else 'empty_result_no_hit_evidence'
            record['success'] = True
            return result
        except BaseException as exc:
            record.update(error_type=type(exc).__name__, error=str(exc)[:500],
                          backend_validation='failed')
            raise
        finally:
            current.reset(token)
            record['seconds'] = round(time.monotonic() - started, 4)
            record['observed_backends'] = sorted(set(record['observed_backends']), key=lambda value: str(value))
            if on_search is not None:
                on_search(copy.deepcopy(record))

    service.kb.search = checked_search
    service.call = controlled_call
    service._benchmark_backend_policy_installed = True
    return description
