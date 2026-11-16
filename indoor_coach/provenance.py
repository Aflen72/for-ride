"""Shared, fail-closed provenance policy for connector, imports and analysis.

A caller's `independent` hint cannot override a known restricted or unknown
source. This checks declared provenance; it cannot authenticate a forged label.
"""
INDEPENDENT_SOURCES = frozenset({
    'UPLOAD', 'MANUAL', 'GARMIN_CONNECT', 'OAUTH_CLIENT', 'DROPBOX',
    'POLAR', 'SUUNTO', 'COROS', 'WAHOO',
})


def canonical_source(value):
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def classify_source(record):
    source = canonical_source(record.get('source'))
    marker = record.get('restricted')
    hint = record.get('source_status')
    if source == 'STRAVA' or marker is True or hint == 'restricted':
        return 'restricted'
    if (marker is not None and marker is not False) or hint not in (None, 'independent'):
        return 'unknown'
    return 'independent' if source in INDEPENDENT_SOURCES else 'unknown'


def enforce_provenance(record):
    result = dict(record)
    result['source'] = canonical_source(record.get('source'))
    status = classify_source(record)
    result['source_status'] = status
    result['restricted'] = {'restricted': True, 'independent': False, 'unknown': None}[status]
    return result
