"""
databricks_client.py — Thin wrapper around Databricks REST APIs.

Provides:
  get()          — authenticated GET
  post()         — authenticated POST (JSON body)
  put_file()     — binary PUT for UC Volumes Files API
  execute_sql()  — run a SQL statement via SQL Warehouse and wait for result
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
SQL_WAIT_TIMEOUT  = '50s'   # Databricks SQL API max is 50s; 60s caused INVALID_PARAMETER_VALUE

# PWAF partner attribution (telemetry-attribution skill, connector-structure
# skill §6). Format: <isv>_<product>/<version>. Connector-level constant —
# never overridable by end users.
USER_AGENT = 'CCTech_ACCConnector/2.0'

# Multi-task sync workflow task keys (create_sync_workflow / run_workflow).
SYNC_DOWNLOAD_TASK_KEY = 'bulk_download'
SYNC_DOWNLOAD_CDC_TASK_KEY = 'bulk_download_cdc'
SYNC_SEED_REGISTRY_TASK_KEY = 'seed_registry'
SYNC_SNAPSHOT_PIPELINE_TASK_KEY = 'snapshot_pipeline'
SYNC_CDC_PIPELINE_TASK_KEY = 'cdc_pipeline'
# Per-task notebook widget — serverless sandbox often omits taskKey tags.
DOWNLOAD_ROLE_SNAPSHOT = 'snapshot'
DOWNLOAD_ROLE_CDC = 'cdc'


def _download_notebook_task(nb_path: str, download_role: str) -> dict:
    """Notebook task with per-task download_role (reliable on serverless)."""
    return {
        'notebook_path': nb_path,
        'source':        'WORKSPACE',
        'base_parameters': {
            'download_role': download_role,
        },
    }


def _sync_workflow_task_keys(download_role: str) -> tuple[str, str]:
    """Return (download_task_key, pipeline_task_key) for create_sync_workflow."""
    if download_role == DOWNLOAD_ROLE_CDC:
        return SYNC_DOWNLOAD_CDC_TASK_KEY, SYNC_CDC_PIPELINE_TASK_KEY
    return SYNC_DOWNLOAD_TASK_KEY, SYNC_SNAPSHOT_PIPELINE_TASK_KEY


def _seed_registry_notebook_path(download_notebook_path: str) -> str:
    """Sibling of bulk_downloader in the same workspace folder."""
    if '/' in download_notebook_path:
        return download_notebook_path.rsplit('/', 1)[0] + '/seed_registry'
    return 'seed_registry'


def _seed_registry_notebook_params(configuration: dict) -> dict[str, str]:
    """Widgets for seed_registry.py — merged into workflow notebook_params."""
    params: dict[str, str] = {}
    for src_key, dest_key in (
        ('acc.catalog', 'acc_catalog'),
        ('acc.dc_project_id', 'acc_dc_project_id'),
        ('acc.dc_snapshot_path', 'acc_dc_snapshot_path'),
        ('acc.dc_cdc_path', 'acc_dc_cdc_path'),
    ):
        val = configuration.get(src_key)
        if val:
            params[dest_key] = str(val)
    return params


class DatabricksClient:
    """
    Stateless HTTP client for one Databricks workspace.
    Constructed per-request from credentials stored in SQLite.
    """

    def __init__(self, workspace_url: str, pat: str):
        self.base = workspace_url.rstrip('/')
        self._headers = {
            'Authorization': f'Bearer {pat}',
            'Content-Type':  'application/json',
            'User-Agent':    USER_AGENT,
        }

    def update_token(self, pat: str) -> None:
        """Swap in a freshly-minted bearer token mid-flight.

        Used by the M2M path, whose Service-Principal tokens (``client_credentials``,
        no refresh grant, ~1h TTL) can expire during a long-running pipeline poll.
        The U2M path never calls this — it constructs a client per request.
        """
        self._headers['Authorization'] = f'Bearer {pat}'

    # ------------------------------------------------------------------
    # Core HTTP methods
    # ------------------------------------------------------------------

    def get(self, path: str, params: dict = None) -> dict:
        url = f'{self.base}{path}'
        r = requests.get(url, headers=self._headers, params=params, timeout=30)
        self._raise(r)
        return r.json()

    def post(self, path: str, body: dict = None) -> dict:
        url = f'{self.base}{path}'
        r = requests.post(url, headers=self._headers, json=body or {}, timeout=60)
        self._raise(r)
        return r.json()

    def put_file(self, path: str, data: bytes, overwrite: bool = True) -> None:
        """
        Upload binary data to a UC Volumes path via the Files API.
        path example: /Volumes/acc_catalog/bronze/acc_bronze_volume/issues/20260420_120000.json
        """
        url = f'{self.base}/api/2.0/fs/files{path}'
        headers = {
            'Authorization': self._headers['Authorization'],
            'Content-Type':  'application/octet-stream',
            'User-Agent':    USER_AGENT,
        }
        params = {'overwrite': 'true'} if overwrite else {}
        r = requests.put(url, headers=headers, params=params, data=data, timeout=120)
        self._raise(r)
        logger.info('Files API: uploaded %d bytes to %s', len(data), path)

    def get_file(self, path: str) -> bytes:
        """Read binary content from a UC Volumes path via the Files API."""
        url = f'{self.base}/api/2.0/fs/files{path}'
        headers = {
            'Authorization': self._headers['Authorization'],
            'User-Agent':    USER_AGENT,
        }
        r = requests.get(url, headers=headers, timeout=120)
        self._raise(r)
        return r.content

    def execute_sql(self, warehouse_id: str, statement: str) -> dict:
        """
        Execute a SQL statement via a SQL Warehouse and wait for completion.
        Returns the full result dict from the Statements API.
        Raises RuntimeError on FAILED or CANCELED state.
        """
        body = {
            'statement':    statement,
            'warehouse_id': warehouse_id,
            'wait_timeout': SQL_WAIT_TIMEOUT,
        }
        result = self.post('/api/2.0/sql/statements', body)
        status = result.get('status', {})
        state  = status.get('state', '')

        # If not yet complete, poll
        if state not in ('SUCCEEDED', 'FAILED', 'CANCELED', 'CLOSED'):
            statement_id = result.get('statement_id')
            result = self._poll_sql(statement_id)
            state  = result.get('status', {}).get('state', '')

        if state != 'SUCCEEDED':
            err = result.get('status', {}).get('error', {})
            raise RuntimeError(
                f'SQL failed [{state}]: {err.get("message", "unknown error")}\nSQL: {statement}'
            )

        logger.info('SQL executed successfully: %.80s', statement)
        return result

    def _poll_sql(self, statement_id: str, max_wait_sec: int = 600) -> dict:
        """Poll the SQL Statements API until a terminal state is reached.
        Default is 600s because a cold-start SQL Warehouse can take up to 5-8 min.
        """
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SEC)
            result = self.get(f'/api/2.0/sql/statements/{statement_id}')
            state  = result.get('status', {}).get('state', '')
            logger.info('SQL statement %s state: %s', statement_id, state)
            if state in ('SUCCEEDED', 'FAILED', 'CANCELED', 'CLOSED'):
                return result
        raise TimeoutError(f'SQL statement {statement_id} did not complete in {max_wait_sec}s')

    # ------------------------------------------------------------------
    # Unity Catalog REST API helpers
    # These bypass SQL DDL and work with Default Storage mode workspaces
    # (new workspaces that don't have a metastore storage root URL set).
    # ------------------------------------------------------------------

    def uc_create_catalog(self, catalog_name: str) -> None:
        """Create a UC catalog via REST API (idempotent).
        Checks existence first via GET to avoid hitting the metastore storage
        validation that occurs even when the catalog already exists.
        """
        try:
            self.get(f'/api/2.1/unity-catalog/catalogs/{catalog_name}')
            logger.info('UC catalog already exists: %s', catalog_name)
            return
        except Exception:
            pass  # catalog does not exist yet — proceed to create

        try:
            self.post('/api/2.1/unity-catalog/catalogs', {'name': catalog_name})
            logger.info('UC catalog created: %s', catalog_name)
        except Exception as exc:
            if 'already exists' in str(exc).lower():
                logger.info('UC catalog already exists: %s', catalog_name)
            else:
                raise

    def uc_create_schema(self, catalog_name: str, schema_name: str) -> None:
        """Create a UC schema via REST API (idempotent)."""
        try:
            self.get(f'/api/2.1/unity-catalog/schemas/{catalog_name}.{schema_name}')
            logger.info('UC schema already exists: %s.%s', catalog_name, schema_name)
            return
        except Exception:
            pass

        try:
            self.post('/api/2.1/unity-catalog/schemas', {
                'name':         schema_name,
                'catalog_name': catalog_name,
            })
            logger.info('UC schema created: %s.%s', catalog_name, schema_name)
        except Exception as exc:
            if 'already exists' in str(exc).lower():
                logger.info('UC schema already exists: %s.%s', catalog_name, schema_name)
            else:
                raise

    def uc_create_volume(self, catalog_name: str, schema_name: str, volume_name: str) -> None:
        """Create a UC managed volume via REST API (idempotent)."""
        try:
            self.get(f'/api/2.1/unity-catalog/volumes/{catalog_name}.{schema_name}.{volume_name}')
            logger.info('UC volume already exists: %s.%s.%s', catalog_name, schema_name, volume_name)
            return
        except Exception:
            pass

        try:
            self.post('/api/2.1/unity-catalog/volumes', {
                'catalog_name': catalog_name,
                'schema_name':  schema_name,
                'name':         volume_name,
                'volume_type':  'MANAGED',
            })
            logger.info('UC volume created: %s.%s.%s', catalog_name, schema_name, volume_name)
        except Exception as exc:
            if 'already exists' in str(exc).lower():
                logger.info('UC volume already exists: %s.%s.%s', catalog_name, schema_name, volume_name)
            else:
                raise

    def uc_list_catalogs(self) -> list:
        """
        List Unity Catalogs visible to the current principal (paginated).
        Returns raw catalog objects from the API (each includes at least 'name').
        """
        out: list = []
        page_token = None
        while True:
            params = {'max_results': 200}
            if page_token:
                params['page_token'] = page_token
            data = self.get('/api/2.1/unity-catalog/catalogs', params=params)
            out.extend(data.get('catalogs') or [])
            page_token = data.get('next_page_token')
            if not page_token:
                break
        return out

    def uc_verify_catalog_exists(self, catalog_name: str) -> None:
        """Raise RuntimeError if the catalog is missing or not accessible."""
        self.get(f'/api/2.1/unity-catalog/catalogs/{catalog_name}')
        logger.info('UC catalog verified: %s', catalog_name)

    def uc_get_catalog(self, catalog_name: str) -> dict:
        """Return the full UC catalog metadata (incl. storage_root, owner, etc.).

        Raises RuntimeError if the catalog is missing or not accessible.
        """
        return self.get(f'/api/2.1/unity-catalog/catalogs/{catalog_name}')

    def uc_get_permissions(self, securable_type: str, full_name: str,
                           principal: str = None, effective: bool = False) -> dict:
        """Return the privilege assignments for a UC securable.

        securable_type: 'catalog' | 'schema' | 'table' | 'volume' (UC kind)
        full_name:      catalog or catalog.schema or catalog.schema.table
        principal:      optional filter — only return grants for this principal
        effective:      ask UC for *effective* privileges instead of direct
                        grants. Effective resolves group membership and
                        inheritance from the parent metastore/catalog, which
                        direct grants do not — required to answer "can this
                        user write here?". A caller without MANAGE may still
                        read effective permissions for their own principal.

        Returns the raw API response, typically:
          { "privilege_assignments": [
              { "principal": "...", "privileges": ["USE_CATALOG", ...] },
              ...
          ]}
        The effective-permissions endpoint reports each privilege as an object
        ({"privilege": "USE_CATALOG", "inherited_from_type": ...}); those are
        flattened to plain strings so both shapes look identical to callers.
        Returns an empty assignments list (rather than raising) when there
        are no grants — the API treats "no grants" as a successful 200 with
        no array key.
        """
        # Allowlist the securable_type to avoid building arbitrary URLs from
        # untrusted input; API only accepts a small set of values anyway.
        allowed = {'catalog', 'schema', 'table', 'volume'}
        if securable_type not in allowed:
            raise ValueError(
                f'Invalid securable_type {securable_type!r}; expected one of {sorted(allowed)}'
            )
        params = {}
        if principal:
            params['principal'] = principal
        endpoint = 'effective-permissions' if effective else 'permissions'
        path = f'/api/2.1/unity-catalog/{endpoint}/{securable_type}/{full_name}'
        try:
            data = self.get(path, params=params or None)
        except Exception as exc:
            logger.warning('uc_get_permissions(%s,%s) failed: %s',
                           securable_type, full_name, exc)
            raise
        # Normalize: always return privilege_assignments key, privileges as strings
        assignments = data.get('privilege_assignments') or []
        for entry in assignments:
            entry['privileges'] = [
                p.get('privilege') if isinstance(p, dict) else p
                for p in entry.get('privileges') or []
            ]
        data['privilege_assignments'] = assignments
        return data

    # ------------------------------------------------------------------
    # Job helpers
    # ------------------------------------------------------------------

    def wait_for_warehouse(self, warehouse_id: str, max_wait_sec: int = 600) -> None:
        """
        Ensure the SQL Warehouse is RUNNING before we submit DDL statements.
        Sends a start request (idempotent) then polls until state == RUNNING.
        Re-sends the start request if the warehouse transitions to STOPPED
        (which happens when Azure fails to provision a VM and Databricks retries).
        Cold starts typically take 3-8 minutes on most Databricks tiers.
        """
        def _start():
            try:
                self.post(f'/api/2.0/sql/warehouses/{warehouse_id}/start', {})
                logger.info('Warehouse %s start requested', warehouse_id)
            except Exception as exc:
                logger.info('Warehouse start request: %s (may already be running)', exc)

        _start()
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            info  = self.get(f'/api/2.0/sql/warehouses/{warehouse_id}')
            state = info.get('state', '')
            logger.info('Warehouse %s state: %s', warehouse_id, state)
            if state == 'RUNNING':
                return
            if state in ('DELETED', 'DELETING'):
                raise RuntimeError(f'Warehouse {warehouse_id} is in state {state} — cannot use it')
            if state == 'STOPPED':
                # Warehouse failed to start (Azure capacity issue) and was stopped.
                # Re-send the start request so Databricks tries again.
                logger.info('Warehouse %s is STOPPED — re-sending start request', warehouse_id)
                _start()
            time.sleep(15)

        raise TimeoutError(f'Warehouse {warehouse_id} did not reach RUNNING in {max_wait_sec}s')

    def _find_job_id_by_name(self, job_name: str) -> int | None:
        """Return job_id for an exact name match, or None."""
        result = self.get('/api/2.1/jobs/list', params={'name': job_name, 'limit': 25})
        for job in result.get('jobs', []):
            settings = job.get('settings') or {}
            if settings.get('name') == job_name:
                return job['job_id']
        return None

    def delete_job(self, job_id: int) -> None:
        """Delete a saved job by id."""
        self.post('/api/2.1/jobs/delete', {'job_id': job_id})
        logger.info('Job deleted (ID: %d)', job_id)

    @staticmethod
    def _job_error_requires_serverless(err: Exception) -> bool:
        return 'only serverless compute is supported' in str(err).lower()

    def get_or_create_job(self, job_name: str, job_config: dict, *, replace: bool = False) -> int:
        """
        Idempotent job creation — update if a job with this name already exists,
        create if not. Returns the job_id.

        ``replace=True`` deletes any existing job first so stale ``job_clusters``
        from a classic spec cannot survive (serverless-only workspaces 400 on reset).
        """
        job_id = self._find_job_id_by_name(job_name)
        if job_id is not None:
            if replace:
                self.delete_job(job_id)
            else:
                self.post('/api/2.1/jobs/reset', {'job_id': job_id, 'new_settings': job_config})
                logger.info('Job "%s" updated (ID: %d)', job_name, job_id)
                return job_id
        resp   = self.post('/api/2.1/jobs/create', job_config)
        job_id = resp['job_id']
        logger.info('Job "%s" created (ID: %d)', job_name, job_id)
        return job_id

    def create_downloader_job(
        self, job_name: str, notebook_path: str, *, replace: bool = True,
    ) -> tuple[int, str]:
        """
        Create the bulk-downloader Job using serverless or classic compute,
        whichever the workspace accepts. Tries serverless first (no clusters),
        then classic job_clusters. Never attempts classic on serverless-only
        workspaces (detected from the API error).
        """
        if replace:
            existing = self._find_job_id_by_name(job_name)
            if existing is not None:
                self.delete_job(existing)
                time.sleep(2)

        def _serverless_task(path: str) -> dict:
            return {
                'task_key': 'download_dc_files',
                'notebook_task': {
                    'notebook_path': path,
                    'source':        'WORKSPACE',
                },
                'timeout_seconds': 3600,
            }

        paths = [notebook_path]
        if notebook_path.startswith('/Users/'):
            paths.append('/Workspace' + notebook_path)

        errors: list[str] = []
        serverless_only = False

        for path in paths:
            cfg = {
                'name': job_name,
                'tasks': [_serverless_task(path)],
                'max_concurrent_runs': 1,
            }
            try:
                job_id = self.post('/api/2.1/jobs/create', cfg)['job_id']
                logger.info('Downloader job created (serverless, ID: %d)', job_id)
                return job_id, 'serverless'
            except Exception as exc:
                errors.append(f'serverless({path}): {exc}')
                if self._job_error_requires_serverless(exc):
                    serverless_only = True

        if not serverless_only:
            classic_config = {
                'name': job_name,
                'tasks': [{
                    'task_key': 'download_dc_files',
                    'notebook_task': {
                        'notebook_path': notebook_path,
                        'source':        'WORKSPACE',
                    },
                    'job_cluster_key': 'downloader_cluster',
                    'timeout_seconds': 3600,
                }],
                'job_clusters': [{
                    'job_cluster_key': 'downloader_cluster',
                    'new_cluster': {
                        'spark_version':      '15.4.x-scala2.12',
                        'node_type_id':       'Standard_D4ds_v5',
                        'num_workers':        0,
                        'data_security_mode': 'SINGLE_USER',
                        'spark_conf':         {'spark.master': 'local[*]'},
                    },
                }],
                'max_concurrent_runs': 1,
            }
            try:
                job_id = self.post('/api/2.1/jobs/create', classic_config)['job_id']
                logger.info('Downloader job created (classic, ID: %d)', job_id)
                return job_id, 'classic'
            except Exception as exc:
                if self._job_error_requires_serverless(exc):
                    serverless_only = True
                    # Workspace is serverless-only; classic was wrong path. Retry
                    # serverless creates (first pass may have failed for other reasons).
                    for path in paths:
                        cfg = {
                            'name': job_name,
                            'tasks': [_serverless_task(path)],
                            'max_concurrent_runs': 1,
                        }
                        try:
                            job_id = self.post('/api/2.1/jobs/create', cfg)['job_id']
                            logger.info(
                                'Downloader job created (serverless retry, ID: %d)', job_id,
                            )
                            return job_id, 'serverless'
                        except Exception as retry_exc:
                            errors.append(f'serverless-retry({path}): {retry_exc}')
                else:
                    errors.append(f'classic: {exc}')

        raise RuntimeError(
            f'Could not create downloader Job "{job_name}" '
            f'(serverless_only_workspace={serverless_only}): '
            + ' | '.join(errors[:3])
        )

    def trigger_job(self, job_id: int, notebook_params: dict | None = None) -> int:
        """Trigger a job run and return the run_id.

        ``notebook_params`` are forwarded to the run-now payload and surface
        inside a notebook task via ``dbutils.widgets.get(<key>)``. Per the
        ``/api/2.1/jobs/run-now`` contract, values must be strings — non-str
        values are coerced here.
        """
        body: dict = {'job_id': job_id}
        if notebook_params:
            body['notebook_params'] = {k: str(v) for k, v in notebook_params.items()}
        resp   = self.post('/api/2.1/jobs/run-now', body)
        run_id = resp['run_id']
        logger.info('Job %d triggered → run_id %d', job_id, run_id)
        return run_id

    def poll_job_run(self, run_id: int, max_wait_min: int = 30) -> str:
        """
        Poll a job run until it reaches a terminal state.
        Returns result_state ('SUCCESS', 'FAILED', etc.).
        Raises TimeoutError if max_wait_min exceeded.
        """
        deadline = time.time() + max_wait_min * 60
        while time.time() < deadline:
            time.sleep(30)
            status    = self.get(f'/api/2.1/jobs/runs/get?run_id={run_id}')
            lc_state  = status.get('state', {}).get('life_cycle_state', '')
            res_state = status.get('state', {}).get('result_state', '')
            logger.info('run_id %d: life_cycle=%s result=%s', run_id, lc_state, res_state)
            if lc_state in ('TERMINATED', 'SKIPPED', 'INTERNAL_ERROR'):
                return res_state or lc_state
        raise TimeoutError(f'Job run {run_id} did not finish in {max_wait_min} minutes')

    def create_sync_workflow(
        self,
        workflow_name: str,
        download_notebook_path: str,
        pipeline_id: str,
        *,
        download_role: str = DOWNLOAD_ROLE_SNAPSHOT,
        full_refresh: bool = False,
        replace: bool = True,
    ) -> tuple[int, str]:
        """Create bulk_downloader → seed_registry → pipeline workflow.

        ``download_role`` is ``snapshot`` (default) or ``cdc`` — must match the
        manifest widget passed at run-now (``manifest_path`` vs ``cdc_manifest_path``).

        CDC sync workflows use ``full_refresh=False`` (incremental merge on a stable
        project stream root). Pass ``full_refresh=True`` only for a one-off rebuild.

        When ``download_role`` is ``cdc``, task keys are ``bulk_download_cdc`` and
        ``cdc_pipeline`` (not the snapshot workflow names).
        """
        if replace:
            existing_id = self._find_job_id_by_name(workflow_name)
            if existing_id is not None:
                self.delete_job(existing_id)
                time.sleep(2)

        download_task_key, pipeline_task_key = _sync_workflow_task_keys(download_role)

        paths = [download_notebook_path]
        if download_notebook_path.startswith('/Users/'):
            paths.append('/Workspace' + download_notebook_path)

        def _tasks(nb_path: str) -> list[dict]:
            seed_path = _seed_registry_notebook_path(nb_path)
            return [
                {
                    'task_key': download_task_key,
                    'notebook_task': _download_notebook_task(nb_path, download_role),
                    'timeout_seconds': 3600,
                },
                {
                    'task_key': SYNC_SEED_REGISTRY_TASK_KEY,
                    'depends_on': [{'task_key': download_task_key}],
                    'notebook_task': {
                        'notebook_path': seed_path,
                        'source':        'WORKSPACE',
                    },
                    'timeout_seconds': 1800,
                },
                {
                    'task_key': pipeline_task_key,
                    'depends_on': [{'task_key': SYNC_SEED_REGISTRY_TASK_KEY}],
                    'pipeline_task': {
                        'pipeline_id':  pipeline_id,
                        'full_refresh': bool(full_refresh),
                    },
                },
            ]

        errors: list[str] = []
        for path in paths:
            cfg = {
                'name':                workflow_name,
                'tasks':               _tasks(path),
                'max_concurrent_runs': 1,
            }
            try:
                job_id = self.post('/api/2.1/jobs/create', cfg)['job_id']
                logger.info('Sync workflow "%s" created (serverless, ID: %d)', workflow_name, job_id)
                return job_id, 'serverless'
            except Exception as exc:
                errors.append(f'serverless({path}): {exc}')

        classic_config = {
            'name': workflow_name,
            'tasks': [
                {
                    'task_key': download_task_key,
                    'notebook_task': _download_notebook_task(
                        download_notebook_path, download_role,
                    ),
                    'job_cluster_key': 'sync_cluster',
                    'timeout_seconds': 3600,
                },
                {
                    'task_key':        SYNC_SEED_REGISTRY_TASK_KEY,
                    'depends_on':      [{'task_key': download_task_key}],
                    'job_cluster_key': 'sync_cluster',
                    'notebook_task': {
                        'notebook_path': _seed_registry_notebook_path(
                            download_notebook_path,
                        ),
                        'source': 'WORKSPACE',
                    },
                    'timeout_seconds': 1800,
                },
                {
                    'task_key':        pipeline_task_key,
                    'depends_on':      [{'task_key': SYNC_SEED_REGISTRY_TASK_KEY}],
                    'job_cluster_key': 'sync_cluster',
                    'pipeline_task': {
                        'pipeline_id':  pipeline_id,
                        'full_refresh': bool(full_refresh),
                    },
                },
            ],
            'job_clusters': [{
                'job_cluster_key': 'sync_cluster',
                'new_cluster': {
                    'spark_version':      '15.4.x-scala2.12',
                    'node_type_id':       'Standard_D4ds_v5',
                    'num_workers':        0,
                    'data_security_mode': 'SINGLE_USER',
                    'spark_conf':         {'spark.master': 'local[*]'},
                },
            }],
            'max_concurrent_runs': 1,
        }
        try:
            job_id = self.post('/api/2.1/jobs/create', classic_config)['job_id']
            logger.info('Sync workflow "%s" created (classic, ID: %d)', workflow_name, job_id)
            return job_id, 'classic'
        except Exception as exc:
            errors.append(f'classic: {exc}')

        raise RuntimeError(
            f'Could not create sync workflow "{workflow_name}": ' + ' | '.join(errors[:3]),
        )

    def create_combined_snapshot_workflow(
        self,
        workflow_name: str,
        snapshot_pipeline_id: str,
        cdc_pipeline_id: str,
        *,
        download_notebook_path: str | None = None,
        replace: bool = True,
    ) -> tuple[int, str]:
        """Combined snapshot workflow: downloads → seed_registry → Pipeline A → B.

        ``bulk_download`` and ``bulk_download_cdc`` run in parallel (serverless),
        then ``seed_registry``, ``snapshot_pipeline``, then ``cdc_pipeline``.
        """
        if replace:
            existing_id = self._find_job_id_by_name(workflow_name)
            if existing_id is not None:
                self.delete_job(existing_id)
                time.sleep(2)

        tasks: list[dict] = []
        download_task_keys: list[str] = []

        if download_notebook_path:
            paths = [download_notebook_path]
            if download_notebook_path.startswith('/Users/'):
                paths.append('/Workspace' + download_notebook_path)
            nb_path = paths[0]
            for task_key in (SYNC_DOWNLOAD_TASK_KEY, SYNC_DOWNLOAD_CDC_TASK_KEY):
                role = (
                    DOWNLOAD_ROLE_CDC
                    if task_key == SYNC_DOWNLOAD_CDC_TASK_KEY
                    else DOWNLOAD_ROLE_SNAPSHOT
                )
                tasks.append({
                    'task_key': task_key,
                    'notebook_task': _download_notebook_task(nb_path, role),
                    'timeout_seconds': 3600,
                })
                download_task_keys.append(task_key)

            seed_path = _seed_registry_notebook_path(nb_path)
            tasks.append({
                'task_key': SYNC_SEED_REGISTRY_TASK_KEY,
                'depends_on': [{'task_key': k} for k in download_task_keys],
                'notebook_task': {
                    'notebook_path': seed_path,
                    'source':        'WORKSPACE',
                },
                'timeout_seconds': 1800,
            })

        snap_dep = [{'task_key': SYNC_SEED_REGISTRY_TASK_KEY}]
        tasks.append({
            'task_key': SYNC_SNAPSHOT_PIPELINE_TASK_KEY,
            'depends_on': snap_dep,
            'pipeline_task': {
                'pipeline_id':  snapshot_pipeline_id,
                'full_refresh': False,
            },
        })
        tasks.append({
            'task_key': SYNC_CDC_PIPELINE_TASK_KEY,
            'depends_on': [{'task_key': SYNC_SNAPSHOT_PIPELINE_TASK_KEY}],
            'pipeline_task': {
                'pipeline_id':  cdc_pipeline_id,
                'full_refresh': True,
            },
        })

        cfg = {
            'name':                workflow_name,
            'tasks':               tasks,
            'max_concurrent_runs': 1,
        }
        job_id = self.post('/api/2.1/jobs/create', cfg)['job_id']
        logger.info(
            'Combined snapshot workflow "%s" created (ID: %d)', workflow_name, job_id,
        )
        return job_id, 'serverless'

    def apply_pipelines_configuration(
        self, pipeline_configs: dict[str, dict],
    ) -> None:
        """Merge configuration into multiple pipelines before a workflow run."""
        for pipeline_id, configuration in pipeline_configs.items():
            self.apply_pipeline_configuration(pipeline_id, configuration)

    def run_combined_workflow(
        self,
        job_id: int,
        *,
        pipeline_configs: dict[str, dict],
        manifest_path: str | None = None,
        cdc_manifest_path: str | None = None,
    ) -> int:
        """Trigger combined snapshot workflow; returns run_id."""
        self.apply_pipelines_configuration(pipeline_configs)
        notebook_params: dict[str, str] = {}
        if manifest_path:
            notebook_params['manifest_path'] = str(manifest_path)
        if cdc_manifest_path:
            notebook_params['cdc_manifest_path'] = str(cdc_manifest_path)
        sample_conf = next(iter(pipeline_configs.values()), {})
        notebook_params.update(_seed_registry_notebook_params(sample_conf))
        body: dict = {'job_id': job_id}
        if notebook_params:
            body['notebook_params'] = notebook_params
        resp = self.post('/api/2.1/jobs/run-now', body)
        run_id = resp['run_id']
        logger.info(
            'Combined workflow %d triggered → run_id %d notebook_params=%s',
            job_id,
            run_id,
            notebook_params,
        )
        for pipeline_id, conf in pipeline_configs.items():
            logger.info(
                '  pipeline %s paths: dc_snapshot=%s dc_cdc=%s',
                pipeline_id,
                conf.get('acc.dc_snapshot_path', ''),
                conf.get('acc.dc_cdc_path', ''),
            )
        return run_id

    @staticmethod
    def _extract_pipeline_run_as(pipeline_data: dict) -> dict | None:
        """Return a PUT-ready ``run_as`` object from GET /pipelines/{id}.

        ``run_as`` is top-level on the GET response (not inside ``spec``).
        Fall back to ``run_as_user_name`` when the explicit object is absent.
        """
        for source in (pipeline_data, pipeline_data.get('spec') or {}):
            if not isinstance(source, dict):
                continue
            run_as = source.get('run_as')
            if isinstance(run_as, dict) and (
                run_as.get('user_name') or run_as.get('service_principal_name')
            ):
                return run_as

        user_name = pipeline_data.get('run_as_user_name')
        if user_name:
            return {'user_name': user_name}
        return None

    def _preserve_pipeline_run_as(
        self, pipeline_id: str, pipeline_body: dict,
    ) -> dict:
        """Ensure PUT payloads retain the pipeline's existing ``run_as``.

        Databricks rejects updates that omit ``run_as`` once it has been
        configured (INVALID_PARAMETER_VALUE: run_as can not be set to null).
        """
        if pipeline_body.get('run_as'):
            return pipeline_body
        data = self.get(f'/api/2.0/pipelines/{pipeline_id}')
        run_as = self._extract_pipeline_run_as(data)
        if not run_as:
            return pipeline_body
        preserved = dict(pipeline_body)
        preserved['run_as'] = run_as
        logger.info(
            'Preserving pipeline %s run_as=%s on update',
            pipeline_id,
            run_as.get('user_name') or run_as.get('service_principal_name'),
        )
        return preserved

    def apply_pipeline_configuration(
        self, pipeline_id: str, configuration: dict,
    ) -> None:
        """Merge per-run pipeline configuration before a workflow's pipeline task.

        Jobs ``run-now`` ``pipeline_params`` only accept refresh flags, not
        ``configuration`` overrides. The pipeline task reads
        ``spark.conf.get('acc.*')`` from the pipeline resource definition.
        """
        data = self.get(f'/api/2.0/pipelines/{pipeline_id}')
        body = data.get('spec') if isinstance(data.get('spec'), dict) else data
        conf = dict(body.get('configuration') or {})
        conf.update({k: str(v) for k, v in configuration.items()})
        body['configuration'] = conf
        if not body.get('run_as'):
            run_as = self._extract_pipeline_run_as(data)
            if run_as:
                body['run_as'] = run_as
        url = f'{self.base}/api/2.0/pipelines/{pipeline_id}'
        r = requests.put(url, headers=self._headers, json=body, timeout=60)
        self._raise(r)
        logger.info('Pipeline %s configuration updated for run', pipeline_id)

    def run_workflow(
        self,
        job_id: int,
        *,
        pipeline_id: str,
        manifest_path: str,
        pipeline_configuration: dict,
        download_role: str = DOWNLOAD_ROLE_SNAPSHOT,
    ) -> int:
        """Trigger a sync workflow run; returns run_id."""
        self.apply_pipeline_configuration(pipeline_id, pipeline_configuration)
        notebook_params = _seed_registry_notebook_params(pipeline_configuration)
        if download_role == DOWNLOAD_ROLE_CDC:
            notebook_params['cdc_manifest_path'] = str(manifest_path)
        else:
            notebook_params['manifest_path'] = str(manifest_path)
        body = {
            'job_id': job_id,
            'notebook_params': notebook_params,
        }
        dc_path = (
            pipeline_configuration.get('acc.dc_snapshot_path')
            or pipeline_configuration.get('acc.dc_cdc_path', '')
        )
        resp = self.post('/api/2.1/jobs/run-now', body)
        run_id = resp['run_id']
        logger.info(
            'Sync workflow %d triggered → run_id %d download_role=%s manifest=%s dc_path=%s',
            job_id,
            run_id,
            download_role,
            manifest_path,
            dc_path,
        )
        return run_id

    @staticmethod
    def _format_workflow_task_line(task: dict) -> str:
        """Single-line summary of one workflow task for poll logs."""
        key = task.get('task_key', '?')
        st = task.get('state', {})
        lc = st.get('life_cycle_state', '')
        res = st.get('result_state', '') or '-'
        msg = (st.get('state_message') or '').replace('\n', ' ').strip()
        if len(msg) > 120:
            msg = msg[:117] + '...'
        dur = task.get('execution_duration')
        dur_s = f' {dur}ms' if dur is not None else ''
        suffix = f' ({msg})' if msg else ''
        return f'{key}={lc}/{res}{dur_s}{suffix}'

    def _log_workflow_task_states(self, run_id: int, status: dict, *, header: str) -> None:
        tasks = status.get('tasks') or []
        if not tasks:
            logger.info('workflow run_id %d: %s — no task details yet', run_id, header)
            return
        lines = [self._format_workflow_task_line(t) for t in tasks]
        logger.info('workflow run_id %d: %s | %s', run_id, header, ' | '.join(lines))

    @staticmethod
    def _workflow_failure_hint(tasks: list[dict]) -> str:
        """Actionable hint when a multi-task workflow fails."""
        by_key = {t.get('task_key'): t for t in tasks}
        download_keys = ('bulk_download', 'bulk_download_cdc')
        failed_downloads = [
            k for k in download_keys
            if k in by_key
            and by_key[k].get('state', {}).get('result_state') not in (None, 'SUCCESS')
        ]
        failed_seed = (
            SYNC_SEED_REGISTRY_TASK_KEY in by_key
            and by_key[SYNC_SEED_REGISTRY_TASK_KEY].get('state', {}).get('result_state')
            not in (None, 'SUCCESS')
        )
        skipped_pipelines = [
            t.get('task_key')
            for t in tasks
            if t.get('task_key', '').endswith('_pipeline')
            and (t.get('state', {}).get('state_message') or '').lower().find('upstream') >= 0
        ]
        hints: list[str] = []
        if failed_downloads:
            hints.append(
                f'download task(s) {failed_downloads} did not succeed — '
                'open their notebook output in Databricks (look for PROGRESS/DONE or HTTP 403 URL expiry)'
            )
        if failed_seed:
            hints.append(
                'seed_registry failed — PK registry / schema.json must be seeded before pipelines '
                '(check pk_config.json on volume and seed_registry notebook output)'
            )
        if skipped_pipelines:
            hints.append(
                f'pipeline task(s) {skipped_pipelines} were skipped — '
                'bronze tables are only created when snapshot_pipeline and cdc_pipeline run to completion'
            )
        if not hints:
            return ''
        return ' Hint: ' + '; '.join(hints) + '.'

    _WORKFLOW_TERMINAL_LC = frozenset({'TERMINATED', 'SKIPPED', 'INTERNAL_ERROR'})

    def get_workflow_run_state(self, run_id: int) -> tuple[str, str]:
        """Return ``(life_cycle_state, result_state)`` for a Databricks job run."""
        status = self.get(f'/api/2.1/jobs/runs/get?run_id={run_id}')
        state = status.get('state', {})
        return (
            state.get('life_cycle_state', '') or '',
            state.get('result_state', '') or '',
        )

    def poll_workflow_run(
        self,
        run_id: int,
        max_wait_min: int = 45,
        before_poll=None,
    ) -> str:
        """Poll a multi-task workflow run until terminal; returns result_state.

        ``max_wait_min`` is an *idle* timeout: the clock resets whenever the run
        makes progress (any change in its life-cycle/result state or in any
        task's state). A long-but-healthy run is therefore never killed by the
        clock; only a run that stops progressing for ``max_wait_min`` minutes
        times out.

        ``before_poll`` is an optional zero-arg callback invoked before each
        poll iteration — U2M sync passes token refresh so long runs survive
        access-token expiry.
        """
        idle_deadline = time.time() + max_wait_min * 60
        last_progress = None
        poll_started = time.time()
        poll_count = 0
        while time.time() < idle_deadline:
            time.sleep(30)
            poll_count += 1
            if before_poll is not None:
                try:
                    before_poll()
                except Exception as exc:
                    logger.warning('poll_workflow_run before_poll hook failed: %s', exc)
            status = self.get(f'/api/2.1/jobs/runs/get?run_id={run_id}')
            state = status.get('state', {})
            lc_state = state.get('life_cycle_state', '')
            res_state = state.get('result_state', '')
            elapsed_min = int((time.time() - poll_started) / 60)
            tasks = status.get('tasks') or []
            progress = (lc_state, res_state, tuple(
                (t.get('task_key'),
                 t.get('state', {}).get('life_cycle_state'),
                 t.get('state', {}).get('result_state'))
                for t in tasks
            ))
            if progress != last_progress:
                self._log_workflow_task_states(
                    run_id,
                    status,
                    header=f'elapsed={elapsed_min}m life_cycle={lc_state} result={res_state or "-"}',
                )
                last_progress = progress
                idle_deadline = time.time() + max_wait_min * 60
            elif poll_count % 4 == 0:
                # Heartbeat every ~2 min when state is unchanged (long bulk_download).
                self._log_workflow_task_states(
                    run_id,
                    status,
                    header=f'elapsed={elapsed_min}m still waiting (idle timeout resets on progress)',
                )
            if lc_state in ('TERMINATED', 'SKIPPED', 'INTERNAL_ERROR'):
                if res_state and res_state != 'SUCCESS':
                    hint = self._workflow_failure_hint(tasks)
                    for task in tasks:
                        tr = task.get('state', {})
                        if tr.get('result_state') not in (None, 'SUCCESS'):
                            key = task.get('task_key', '?')
                            msg = tr.get('state_message', '')
                            logger.error('Task %s failed: %s', key, msg)
                    if hint:
                        logger.error('Workflow run %d:%s', run_id, hint)
                elif res_state == 'SUCCESS':
                    self._log_workflow_task_states(
                        run_id, status, header=f'completed in ~{elapsed_min}m',
                    )
                return res_state or lc_state
        raise TimeoutError(
            f'Workflow run {run_id} made no progress for {max_wait_min} minutes '
            f'(elapsed ~{int((time.time() - poll_started) / 60)}m). '
            f'First full sync often needs 60–120+ minutes — increase SYNC_WORKFLOW_MAX_WAIT_MIN '
            f'or check bulk_download notebook output in Databricks.',
        )

    # ------------------------------------------------------------------
    # Lakeflow Pipelines (Spark Declarative Pipelines / DLT)
    # Bronze AUTO CDC FROM SNAPSHOT pipeline replaces the legacy
    # bronze ingestion Job. Pipelines API requires Pro / Advanced or
    # Serverless workspace edition — Standard returns 400 here.
    # ------------------------------------------------------------------

    def get_pipeline_by_name(self, name: str) -> dict | None:
        """Return the first pipeline matching ``name`` exactly, or None.

        ``GET /api/2.0/pipelines`` supports a ``filter`` query — name LIKE
        is used because the API has no exact-match operator. We then verify
        the returned ``name`` field equals the requested one to avoid
        accidental prefix matches.
        """
        page_token = None
        while True:
            params = {'filter': f"name LIKE '{name}'", 'max_results': 100}
            if page_token:
                params['page_token'] = page_token
            data = self.get('/api/2.0/pipelines', params=params)
            for p in (data.get('statuses') or []):
                if p.get('name') == name:
                    return p
            page_token = data.get('next_page_token')
            if not page_token:
                return None

    def get_or_create_pipeline(self, name: str, pipeline_config: dict) -> str:
        """Idempotently create a Lakeflow Pipeline by name. Returns pipeline_id.

        If a pipeline with the same name already exists, its definition is
        updated via PUT (mirrors ``get_or_create_job``'s ``reset`` semantics).
        """
        existing = self.get_pipeline_by_name(name)
        if existing:
            pipeline_id = existing['pipeline_id']
            body = self._preserve_pipeline_run_as(pipeline_id, pipeline_config)
            url = f'{self.base}/api/2.0/pipelines/{pipeline_id}'
            r = requests.put(url, headers=self._headers, json=body, timeout=60)
            self._raise(r)
            logger.info('Pipeline "%s" updated (ID: %s)', name, pipeline_id)
            return pipeline_id
        resp = self.post('/api/2.0/pipelines', pipeline_config)
        pipeline_id = resp['pipeline_id']
        logger.info('Pipeline "%s" created (ID: %s)', name, pipeline_id)
        return pipeline_id

    def delete_pipeline(self, pipeline_id: str) -> None:
        """Delete a Lakeflow pipeline. A missing pipeline is not an error."""
        url = f'{self.base}/api/2.0/pipelines/{pipeline_id}'
        r = requests.delete(url, headers=self._headers, timeout=60)
        if r.status_code == 404:
            logger.info('Pipeline %s already absent — nothing to delete', pipeline_id)
            return
        self._raise(r)
        logger.info('Pipeline %s deleted', pipeline_id)

    def delete_pipeline_by_name(self, name: str) -> str | None:
        """Delete a pipeline looked up by name; returns its ID, or None if absent.

        Refuses while an update is in flight — deleting mid-update would abort
        a running ingestion and leave the bronze tables half-written.
        """
        existing = self.get_pipeline_by_name(name)
        if not existing:
            return None
        pipeline_id = existing['pipeline_id']
        if self.pipeline_has_active_update(pipeline_id):
            raise RuntimeError(
                f'Pipeline "{name}" ({pipeline_id}) has an update in progress. '
                f'Wait for it to finish, then retry.'
            )
        self.delete_pipeline(pipeline_id)
        return pipeline_id

    def start_pipeline_update(self, pipeline_id: str, full_refresh: bool = False,
                              configuration: dict | None = None) -> str:
        """Trigger a pipeline update; returns the update_id.

        ``configuration`` merges into the pipeline update (overrides default
        pipeline configuration for this run only). Used to pass
        ``acc.dc_snapshot_path`` so AUTO CDC FROM SNAPSHOT reads one DC drop.
        """
        body = {'full_refresh': bool(full_refresh)}
        if configuration:
            body['configuration'] = configuration
        resp = self.post(f'/api/2.0/pipelines/{pipeline_id}/updates', body)
        update_id = resp['update_id']
        logger.info('Pipeline %s update triggered → update_id %s', pipeline_id, update_id)
        return update_id

    def get_pipeline_update(self, pipeline_id: str, update_id: str) -> dict:
        """Return the current ``update`` payload for a pipeline update."""
        return self.get(f'/api/2.0/pipelines/{pipeline_id}/updates/{update_id}')

    _PIPELINE_UPDATE_TERMINAL = frozenset({'COMPLETED', 'FAILED', 'CANCELED'})

    def pipeline_has_active_update(self, pipeline_id: str) -> bool:
        """Return True if any recent pipeline update is still in progress."""
        page_token = None
        while True:
            params = {'max_results': 25}
            if page_token:
                params['page_token'] = page_token
            data = self.get(f'/api/2.0/pipelines/{pipeline_id}/updates', params=params)
            for entry in (data.get('updates') or []):
                state = (entry.get('state') or '').upper()
                if state and state not in self._PIPELINE_UPDATE_TERMINAL:
                    return True
            page_token = data.get('next_page_token')
            if not page_token:
                return False

    def poll_pipeline_update(self, pipeline_id: str, update_id: str,
                             max_wait_min: int = 30, before_poll=None) -> str:
        """Poll a pipeline update until it reaches a terminal state.

        Returns the final ``state`` string (COMPLETED / FAILED / CANCELED).

        ``max_wait_min`` is an *idle* timeout: the clock resets whenever the
        update's state changes, so a long-but-healthy run is never killed by the
        clock; only an update that stops progressing for ``max_wait_min``
        minutes raises TimeoutError.

        ``before_poll`` is an optional zero-arg callback invoked before each
        poll iteration — the M2M path uses it to re-mint and re-apply a fresh
        Service-Principal token so long baseline runs don't fail on token
        expiry. U2M callers omit it.
        """
        terminal = {'COMPLETED', 'FAILED', 'CANCELED'}
        idle_deadline = time.time() + max_wait_min * 60
        last_state = None
        while time.time() < idle_deadline:
            time.sleep(15)
            if before_poll is not None:
                try:
                    before_poll()
                except Exception as exc:
                    logger.warning('poll_pipeline_update before_poll hook failed: %s', exc)
            payload = self.get_pipeline_update(pipeline_id, update_id)
            update  = payload.get('update', payload)
            state   = (update.get('state') or '').upper()
            logger.info('Pipeline %s update %s state: %s',
                        pipeline_id, update_id, state)
            if state in terminal:
                return state
            # Reset the idle clock whenever the update's state changes.
            if state != last_state:
                last_state = state
                idle_deadline = time.time() + max_wait_min * 60
        raise TimeoutError(
            f'Pipeline {pipeline_id} update {update_id} made no progress for '
            f'{max_wait_min} minutes'
        )

    # ------------------------------------------------------------------
    # Workspace helpers
    # ------------------------------------------------------------------

    def get_current_user_email(self) -> str:
        """Return the email of the authenticated Databricks user."""
        data = self.get('/api/2.0/preview/scim/v2/Me')
        return data.get('userName', '')

    def mkdirs(self, path: str) -> None:
        """Create a workspace folder (and all parents) if it does not exist."""
        self.post('/api/2.0/workspace/mkdirs', {'path': path})

    def import_notebook(self, path: str, content_b64: str) -> None:
        """Upload a Python notebook, overwriting any existing file at that path."""
        self.post('/api/2.0/workspace/import', {
            'path':      path,
            'format':    'SOURCE',
            'language':  'PYTHON',
            'content':   content_b64,
            'overwrite': True,
        })

    def validate_volume_path(self, volume_path: str) -> bool:
        """
        Confirm the UC Volume path is accessible via the Files API.
        volume_path example: /Volumes/acc_catalog/bronze/acc_bronze_volume
        """
        try:
            self.get(f'/api/2.0/fs/directories{volume_path}')
            return True
        except Exception as exc:
            logger.warning('Volume path %s not accessible: %s', volume_path, exc)
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _raise(response: requests.Response) -> None:
        if not response.ok:
            raise RuntimeError(
                f'Databricks API error {response.status_code}: {response.text[:400]}'
            )
