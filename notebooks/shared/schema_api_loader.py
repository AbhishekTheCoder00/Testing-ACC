# Shared by seed_registry (%run) — parallel APS Schema API fetch.
import concurrent.futures

DEFAULT_SCHEMA_API_PARALLELISM = 8
DEFAULT_SCHEMA_API_TIMEOUT_SEC = 30


def _is_three_level_schema_doc(doc: dict) -> bool:
    for sch_val in doc.values():
        if not isinstance(sch_val, dict):
            continue
        for tbl_val in sch_val.values():
            if not isinstance(tbl_val, dict):
                continue
            for col_val in tbl_val.values():
                return (
                    isinstance(col_val, dict)
                    and 'ordinal_position' in col_val
                )
    return False


def _is_two_level_schema_doc(doc: dict) -> bool:
    if not isinstance(doc, dict):
        return False
    for tbl_val in doc.values():
        if not isinstance(tbl_val, dict):
            continue
        for col_val in tbl_val.values():
            return (
                isinstance(col_val, dict)
                and 'ordinal_position' in col_val
            )
    return False


def _fetch_schema_group(
    group: str,
    schema_endpoint: str,
    *,
    get_fn,
    timeout: int,
) -> tuple[str, dict | None, str | None]:
    url = f'{schema_endpoint}?name={group}&format=json'
    try:
        resp = get_fn(url, timeout=timeout)
        resp.raise_for_status()
        doc = resp.json()
    except Exception as exc:
        return group, None, str(exc)
    if _is_two_level_schema_doc(doc):
        return group, {group: doc}, None
    if _is_three_level_schema_doc(doc):
        return group, doc, None
    return group, None, 'unrecognized shape'


def load_schema_doc_from_api(
    service_groups: list[str],
    schema_endpoint: str,
    *,
    max_workers: int = DEFAULT_SCHEMA_API_PARALLELISM,
    timeout: int = DEFAULT_SCHEMA_API_TIMEOUT_SEC,
    get_fn=None,
) -> dict:
    """Fetch schema docs for all service groups in parallel; merge into one dict."""
    if get_fn is None:
        import requests
        get_fn = requests.get

    merged: dict = {}
    skipped: list[str] = []
    workers = max(1, min(max_workers, len(service_groups) or 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _fetch_schema_group,
                group,
                schema_endpoint,
                get_fn=get_fn,
                timeout=timeout,
            )
            for group in service_groups
        ]
        for fut in concurrent.futures.as_completed(futures):
            group, fragment, err = fut.result()
            if err:
                skipped.append(group)
                print(
                    f'[seed_registry] Schema API: {group} unavailable ({err}) — skipped'
                )
                continue
            merged.update(fragment)

    print(
        f'[seed_registry] Schema API: loaded {len(merged)} group(s), '
        f'skipped {len(skipped)}'
    )
    if skipped:
        print(f'[seed_registry] Schema API: skipped groups — {", ".join(skipped)}')
        raise RuntimeError(
            f'Schema API returned an incomplete schema — {len(skipped)} of '
            f'{len(service_groups)} group(s) failed: {", ".join(skipped)}'
        )
    if not merged:
        raise RuntimeError(
            f'No schemas returned from APS Schema API — all '
            f'{len(service_groups)} group(s) failed'
        )
    return merged
