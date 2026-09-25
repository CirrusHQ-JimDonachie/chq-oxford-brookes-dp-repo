# Lambda handlers

Source for every Lambda function in the Oxford Brookes building data platform.
Each handler is a directory here; its `app/` subdirectory is the deployment
package, and everything else in the directory (tests, the per-handler
`conftest.py`, `requirements.txt` and the `required_*.json` files) stays out of
it.

| Handler | Trigger | Timeout | Runtime dependencies |
| --- | --- | --- | --- |
| `fn-transform` | S3 object-created notification on the `raw/` prefix of the landing bucket | 300 s | `aws-lambda-powertools`, `pyiceberg`, `pyarrow`, bundled in the package (see `fn-transform/requirements.txt`); no layer |

## Running the checks

From this directory:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict fn-transform
uv run pytest fn-transform -v --disable-socket
uv run pip-audit -r fn-transform/requirements.txt
```

`mypy` and `pytest` run per handler. Every handler has an `app/handler.py` and
a `tests/test_handler.py`, so a single run over the whole directory collides on
module names.

`pyproject.toml` here is development-only: the ruff, mypy and pytest
configuration and the pinned tool versions. Nothing in it is deployed.

## Handing over to the infrastructure

Each handler declares what it needs in four files the CloudFormation template
reads:

| File | What it carries |
| --- | --- |
| `required_api_actions.json` | Every IAM action the handler calls, with the project resource each targets |
| `required_env_vars.json` | Environment variable names and purposes; values are set at deploy time |
| `required_event_source_config.json` | Resolved settings the trigger and the function need: timeout, memory, notification filter, failure destination, table attributes |
| `required_runtime.json` | Libraries the package bundles or a layer supplies, and whether tracing is on |

The handler's own README covers its behaviour, its configuration items and the
two Lake Formation prerequisites that sit outside the function role.
