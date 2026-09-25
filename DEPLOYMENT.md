# Deploying the building data platform

One stack, `main.yaml`, with five nested children. Everything is in eu-west-2.
The Customer's own pipeline deploys it; these notes cover what that pipeline has
to do and what has to be true of the account before the first deployment.

## What the pipeline has to do

**Package before deploying, with SAM CLI, not plain `aws cloudformation
package`.** The children are referenced by local path, so they need uploading
first; the transform function additionally needs a Docker image built and
pushed to ECR before the stack can reference it. Building and pushing that
image via the `Metadata: {Dockerfile, DockerContext, DockerTag}` block on
`TransformFunction` (`nested/engine.yaml`) is a SAM CLI capability —
confirmed by testing directly, not assumed: plain `aws cloudformation package`
has no `--image-repository` option and no Docker/ECR support at all, only
`sam build`/`sam package` do. SAM CLI is therefore a real tool prerequisite
now, not optional tooling:

```bash
sam build --template-file main.yaml --region eu-west-2
sam package \
  --s3-bucket <artifact-bucket> \
  --image-repository <account-id>.dkr.ecr.eu-west-2.amazonaws.com/<project>-<environment>-transform \
  --output-template-file packaged.yaml \
  --region eu-west-2
sam deploy \
  --template-file packaged.yaml \
  --stack-name <project>-<environment> \
  --region eu-west-2 \
  --parameter-overrides file://parameters.json \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
```

`CAPABILITY_IAM` covers every role the stacks create except one.
`CAPABILITY_NAMED_IAM` is for that one: `analytics.yaml`'s `FlatRefreshRole` is
named deterministically, so `foundation.yaml`'s `DataLakeSettings` can add it
to the Lake Formation admins list by ARN without a forward cross-stack
reference (see the comment there). `CAPABILITY_AUTO_EXPAND` covers the SAM
transform on the transform function — a template-level expansion CloudFormation
does server-side, unrelated to and not a substitute for the image build above.

**The ECR repository has to exist before packaging, and isn't stack-managed.**
Same reason the S3 artifact bucket isn't: `sam package` needs somewhere to
push the image to before the stack deploy that would otherwise create it even
runs. Create it once, out of band:

```bash
aws ecr create-repository \
  --repository-name <project>-<environment>-transform \
  --region eu-west-2 \
  --image-scanning-configuration scanOnPush=true \
  --encryption-configuration encryptionType=KMS
```

**Why container image, not zip.** It wasn't the first approach. A zip package
built with `pip install --target lambda/fn-transform/app/ --platform
manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --python-version 3.14
--only-binary=:all:` (both `--platform` flags needed: pip's cross-platform
matching requires an exact tag match per flag, and pyarrow ships only a
`manylinux_2_28_x86_64` wheel while pydantic-core, pulled in by pyiceberg,
ships only `manylinux_2_17_x86_64`/`manylinux2014_x86_64`) worked and passed
CI-equivalent testing, landing at 242 MB — under the 250 MB uncompressed
function-code-plus-layers limit, but by only ~8 MB, with `boto3`/`botocore`
already required (the app's own `pyproject.toml` pins `boto3>=1.43.95` as a
direct dependency, not just something `pyiceberg`'s `rest-sigv4` extra pulls
in, so pruning them to save space isn't safe here) and `pyarrow` (156 MB) not
reducible via pip: PyPI ships one full build with Parquet, Datasets, Acero and
the compute engine included; conda-forge's smaller `pyarrow-core`/`pyarrow`/
`pyarrow-all` split, and its custom-selection mechanism for picking individual
components, are both conda-only with no pip equivalent. The container image's
10 GB limit removes the ceiling entirely, and the build is a native `pip
install` inside the image — no cross-platform wheel-tag matching to get wrong.

**Application source lives in this repo.** `lambda/fn-transform/app/` holds the
ten source modules (`handler.py`, `transform.py`, `config.py`, `mapping.py`,
`state.py`, `alerting.py`, `iceberg_writer.py`, `errors.py`, `exceptions.py`,
`reasons.py`); dependencies are installed by the `Dockerfile` at build time,
not pre-installed on the host. This used to be a hand-synced copy of a
separate dev project's source — as of this repo's creation the two were
unified, so `lambda/` here is the single canonical copy: its own `uv` project
(`pyproject.toml`, `uv.lock`), its own tests (`lambda/fn-transform/tests/`),
and the `Dockerfile`/`requirements.txt` the container build needs, all in one
place. No sync step, no sibling directory to keep in step with.

## What the deploy role needs

These permissions are on the Customer's CloudFormation execution role. The stack
cannot grant them to itself, and a deployment without them fails part way.

| Permission | Needed for |
| --- | --- |
| `lakeformation:RegisterResource`, `lakeformation:RegisterResourceWithPrivilegedAccess` | Registering the table buckets with Lake Formation |
| `lakeformation:CreateCatalog`, `glue:CreateCatalog` | Creating the federated catalog |
| `lakeformation:GrantPermissions`, `lakeformation:PutDataLakeSettings` | The data lake settings and the four grants |
| `iam:PassRole` on the Lake Formation vending role | Handing that role to Lake Formation at registration |
| `kms:GenerateDataKeyWithoutPlaintext`, `kms:Decrypt`, `kms:Encrypt`, `kms:ReEncryptFrom`, `kms:ReEncryptTo`, `kms:DescribeKey` on the data key | Creating and updating the encrypted event bus, and every rule operation on it |
| `ecr:GetAuthorizationToken` | Docker login to the registry; account-level, not scoped to one repository |
| `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage` on the transform function's repository | `sam package` pushing the built image |

The role also has to be a Lake Formation data lake administrator. The foundation
stack makes it one, as `DeployRoleArn`, but the first deployment writes the four
grants in the same run, so an account where Lake Formation has never been
configured needs the administrator set by hand before the first deployment. Once
the stack has run, the setting maintains itself.

## Account settings this stack takes over

Three settings hold one value per account per region. Read each before the first
deployment and check nothing else in the account depends on the current value.

```bash
aws lakeformation get-data-lake-settings --region eu-west-2
aws glue get-catalog --catalog-id s3tablescatalog --region eu-west-2
aws apigateway get-account --region eu-west-2
```

**Lake Formation data lake settings.** Deploying replaces the administrator
list with the deploy role. An administrator another workload added is dropped.
Deleting the stack leaves the settings empty.

**The `s3tablescatalog` Glue catalog.** It mounts every S3 table bucket in the
region, including buckets this project did not create. It is retained when the
stack is deleted, so removing the platform does not take the Glue and Athena
view of other teams' table buckets away with it. Retiring it is a deliberate
two-step: remove the retain policies in their own deployment, then delete.

**The API Gateway CloudWatch Logs role.** Every API in the region writes logs
under whichever role is set here, and deploying replaces it. Deleting the stack
does not revoke the access, because API Gateway can still assume a role it was
handed; but the role itself goes with the stack, so a teardown leaves the
account pointing at an ARN that no longer resolves. Set it to another role, or
clear it, as part of any teardown.

## Prerequisites outside the stack

- **QuickSight.** The account has to be subscribed to QuickSight Enterprise in
  eu-west-2, with at least one Author Pro user, before the analytics stack
  deploys. `QuickSightServiceRoleArn` and `QuickSightPrincipalArn` come from
  that subscription. Datasets, dashboards and the Q topic are built in the
  console on top of the data source this stack creates.
- **The alert e-mail subscription** sits in PendingConfirmation until the
  recipient follows the link AWS sends. Nothing is delivered until they do.
  Confirm one notification end to end before treating alerting as working.
- **Read API users.** The Supplier creates the initial users with
  `AdminCreateUser` against the pool this stack creates. Sign-up is closed.
- **SAM CLI and a running Docker daemon**, wherever `package`/`deploy` runs.
  Building the transform function's image is a `sam build`/`sam package`
  capability, not something plain `aws cloudformation package` has any part
  of — confirmed directly, not assumed from documentation.

## Deployment order inside the stack

CloudFormation works this out from the references, but it is worth knowing:

```
FoundationStack            key, Lake Formation settings, catalog
  |-- StorageStack         curated table bucket, Athena results bucket
  |     |-- EngineStack    landing bucket, function, tables, alerting
  |     |     |-- ApiStack Cognito, REST API
  |     |-- AnalyticsStack Athena workgroup, QuickSight data source
```

## Things to watch on the first deployment and the first file drop

Each of these is correct as far as the AWS documentation goes, and each fails in
a way no template validation can catch.

**The curated write may need a second Lake Formation setting.** The foundation
stack sets `AllowFullTableExternalDataAccess: true` and
`AllowExternalDataFiltering: false`. AWS documents these as two alternative ways
of vending credentials and does not say plainly which one governs an external
Iceberg client of the kind the transform function uses. If the first curated
write fails with an access error from the Glue Iceberg endpoint, turning
`AllowExternalDataFiltering` on, with `ExternalDataFilteringAllowList` naming
this account, is the first thing to try.

**Breach alerts may not reach the topic.** The topic policy scopes the
EventBridge publish with `aws:SourceArn` set to the rule ARN, which is what the
confused-deputy documentation prescribes. No page confirms that EventBridge
populates that key on a publish to SNS. If it does not, the condition denies the
publish: the rule still matches, the deployment is still green, and no e-mail
arrives. The end-to-end alert test covers this. If the e-mail does not land,
change that statement's condition to `aws:SourceAccount` alone.

**The read API may return 500 on every request.** The integration role's trust
policy conditions on `aws:SourceAccount`. API Gateway publishes no
confused-deputy guidance, so there is no statement that it populates the key
when assuming an integration role. The first request through the API settles it.

**Two smaller ones.** The CLI walkthrough for the S3 Tables integration passes
`--with-privileged-access` when registering the buckets;
`AWS::LakeFormation::Resource` has no property for it, so it is not set. If the
integration turns out to need it, re-register out of band. And
`quicksight:PassDataSource` appears in AWS's own permissions example but not in
the Service Authorization Reference; if it has been retired the data source
create fails at deploy with an invalid-action error.

## The state table's secondary index

The HLD anticipates a secondary index on the state table for a dashboard access
pattern: listing every entity of one type without knowing each entity's key up
front. `EntityTypeIndex` (`entity_type` hash key, `pk` range key, full
projection) is that index. `entity_type` is the mapping config's `entity.type`
(`nested/engine.yaml`'s `StateTable`), written onto every snapshot item by
`upsert_snapshot` (`state.py`); claim and alert-state items carry no
`entity_type`, so the index only ever holds current snapshots. No consumer
queries it yet — the read API still only does `GetItem` by room ID
(`nested/api.yaml`) — it is there for the dashboard/QuickSight access pattern
the HLD names, and for any future source whose mapping config gives entities a
different `entity.type` to list by.

## Testing the pipeline end to end

Every command below was run against the live sandbox stack (`building-data-dev`,
account `421454275355`, `eu-west-2`) to prove the platform, not copied from
documentation. They assume the AWS CLI is configured for that account and `jq`
is installed. Run the lookups in order — later steps reuse the shell variables
the earlier ones set.

### 0. Look up the stack's resource names

Most names are stable stack outputs; the two DynamoDB tables are not (no
output names them), so they need one extra hop through the nested `EngineStack`:

```bash
STACK=building-data-dev
REGION=eu-west-2

OUTPUTS=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs" --output json)
RAW_BUCKET=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="RawLandingBucketName").OutputValue')
READ_API_URL=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="ReadApiInvokeUrl").OutputValue')
USER_POOL_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="UserPoolId").OutputValue')
USER_POOL_CLIENT_ID=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="UserPoolClientId").OutputValue')
TRANSFORM_FUNCTION=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="TransformFunctionName").OutputValue')

ENGINE_STACK_ID=$(aws cloudformation describe-stack-resource --stack-name "$STACK" \
  --logical-resource-id EngineStack --region "$REGION" \
  --query "StackResourceDetail.PhysicalResourceId" --output text)
STATE_TABLE=$(aws cloudformation describe-stack-resources --stack-name "$ENGINE_STACK_ID" --region "$REGION" \
  --query "StackResources[?LogicalResourceId=='StateTable'].PhysicalResourceId" --output text)
CONFIG_TABLE=$(aws cloudformation describe-stack-resources --stack-name "$ENGINE_STACK_ID" --region "$REGION" \
  --query "StackResources[?LogicalResourceId=='ConfigTable'].PhysicalResourceId" --output text)
```

### 1. Seed the mapping config (once per fresh `ConfigTable`)

`ConfigTable` retains across a stack delete/recreate (`RetainExceptOnCreate`),
but a genuinely new one starts empty — the transform function does nothing
declaratively without this item. This is the worked example's `bms/environmental`
config, matching `conftest.py`'s `mapping_config_item()` fixture in the dev
project:

```bash
cat > /tmp/mapping-config.json <<'EOF'
{
  "pk": {"S": "config#bms#environmental"},
  "sk": {"S": "mapping"},
  "input_format": {"S": "csv"},
  "has_header": {"BOOL": true},
  "fields": {"L": [
    {"M": {"from": {"S": "SensorRef"}, "to": {"S": "room_id"}, "type": {"S": "string"}}},
    {"M": {"from": {"S": "Timestamp"}, "to": {"S": "reading_at"}, "type": {"S": "timestamp"}, "format": {"S": "epoch_ms"}}},
    {"M": {"from": {"S": "RoomTemp"}, "to": {"S": "temperature_c"}, "type": {"S": "decimal"}, "scale": {"S": "0.1"}}},
    {"M": {"from": {"S": "RH"}, "to": {"S": "humidity_pct"}, "type": {"S": "decimal"}}}
  ]},
  "validation": {"M": {
    "required": {"L": [{"S": "room_id"}, {"S": "reading_at"}]},
    "ranges": {"M": {"temperature_c": {"M": {"min": {"S": "-20"}, "max": {"S": "60"}}}}}
  }},
  "entity": {"M": {"type": {"S": "room"}, "key_field": {"S": "room_id"}}},
  "target_table": {"S": "building_data.bms_environmental"}
}
EOF
aws dynamodb put-item --table-name "$CONFIG_TABLE" --item "file:///tmp/mapping-config.json" --region "$REGION"
```

### 2. Drop a test file

The raw bucket's bucket policy denies any upload that doesn't carry the
`aws:kms` encryption header — `--sse aws:kms` is not optional:

```bash
DATE_PATH=$(date -u +%Y/%m/%d)
NOW_MS=$(($(date +%s%N)/1000000))
cat > /tmp/test-drop.csv <<EOF
SensorRef,Timestamp,RoomTemp,RH
NHHB-2.14,$NOW_MS,215,47.5
EOF
aws s3 cp /tmp/test-drop.csv "s3://${RAW_BUCKET}/raw/bms/environmental/${DATE_PATH}/test-drop.csv" \
  --region "$REGION" --sse aws:kms
```

### 3. Confirm the transform succeeded

The engine logs one structured `object.transform` line per file, at INFO —
filtering it out of the DEBUG noise around it is worth doing:

```bash
sleep 15
aws logs tail "/aws/lambda/${TRANSFORM_FUNCTION}" --region "$REGION" --since 2m --format short \
  | grep '"operation":"object.transform"'
```

Look for `"outcome":"success"`, `"rows_written":1`, `"records_rejected":0`. Any
`records_rejected` > 0 means a row failed validation; check `raw/error/...` in
the same bucket for the rejected record and its machine-readable reason code.

### 4. Confirm the state-table snapshot (hot path)

```bash
aws dynamodb get-item --table-name "$STATE_TABLE" --region "$REGION" \
  --key '{"pk":{"S":"room#NHHB-2.14"},"sk":{"S":"snapshot"}}'
```

`temperature_c` should read `21.5` (`215` × the config's `0.1` scale) and
`humidity_pct` should read `47.5`.

### 5. Confirm the `EntityTypeIndex` GSI is queryable

```bash
aws dynamodb query --table-name "$STATE_TABLE" --index-name EntityTypeIndex --region "$REGION" \
  --key-condition-expression "entity_type = :t" \
  --expression-attribute-values '{":t":{"S":"room"}}'
```

This is a sparse index — only snapshot items carry `entity_type`, so a room
whose only write predates the GSI (or predates this session's `entity_type`
addition to `upsert_snapshot`) won't appear until it's re-dropped.

### 6. Prove the alerting mechanism (threshold breach)

Alert rules live in the config table, keyed `rule#{entity_pk}` with the metric
name as the sort key (`alerting.py`'s `_parse_rule`). Seed a rule the test file
above will breach, then re-drop:

```bash
aws dynamodb put-item --table-name "$CONFIG_TABLE" --region "$REGION" --item '{
  "pk": {"S": "rule#room#NHHB-2.14"},
  "sk": {"S": "temperature_c"},
  "comparison": {"S": ">"},
  "threshold": {"N": "20.0"},
  "severity": {"S": "warning"}
}'

# Re-drop the same reading now that a rule exists for it (repeat step 2, or
# just re-run its aws s3 cp with a fresh filename/timestamp).
```

Then check the alert-state item transitioned to `BREACH` (state table, sort
key `alert#{metric}`) and, if a real email subscription is confirmed on the
alert topic, that the notification arrived:

```bash
aws dynamodb get-item --table-name "$STATE_TABLE" --region "$REGION" \
  --key '{"pk":{"S":"room#NHHB-2.14"},"sk":{"S":"alert#temperature_c"}}'
```

A second file with the same reading should **not** re-trigger the email — the
engine only notifies on an `OK`→`BREACH` transition, not on every breaching
read. Drop a file with a value back under the threshold to see the state
reset to `OK` (with a recovery notification, if configured).

### 7. Confirm the curated Iceberg write (cold path)

Only a Lake Formation data-lake administrator can currently query the
`s3tablescatalog` federated catalog directly (see "One thing worth knowing"
below) — as that principal:

```bash
QID=$(aws athena start-query-execution --region "$REGION" \
  --work-group "$STACK" \
  --query-string 'SELECT * FROM "s3tablescatalog/building-data-dev-curated"."building_data"."bms_environmental"' \
  --query "QueryExecutionId" --output text)
aws athena get-query-execution --query-execution-id "$QID" --region "$REGION" \
  --query "QueryExecution.Status.State" --output text
# once SUCCEEDED:
aws athena get-query-results --query-execution-id "$QID" --region "$REGION"
```

### 8. Refresh the QuickSight flat table and SPICE dataset manually

The hourly schedule does this automatically; to see it happen without waiting:

```bash
aws stepfunctions start-execution --region "$REGION" \
  --state-machine-arn "arn:aws:states:${REGION}:421454275355:stateMachine:${STACK}-flat-refresh" \
  --name "manual-test-$(date +%s)"
```

Poll `aws stepfunctions describe-execution --execution-arn <arn from above>`
until `status` is `SUCCEEDED`, then check the SPICE ingestion it triggered:

```bash
aws quicksight list-ingestions --aws-account-id 421454275355 \
  --data-set-id "${STACK}-bms-environmental" --region "$REGION" \
  --query "Ingestions[0].{Status:IngestionStatus,Rows:RowInfo.RowsIngested}"
```

### 9. Test the read API (Cognito SRP + curl)

The app client only allows `ALLOW_USER_SRP_AUTH` (`nested/api.yaml`) — no
`ADMIN_USER_PASSWORD_AUTH`, so plain `aws cognito-idp initiate-auth` cannot
authenticate on its own; SRP needs an actual SRP implementation. `pycognito`
does this in a few lines:

```bash
pip install pycognito boto3
python3 - <<'EOF'
from pycognito import Cognito

USER_POOL_ID = "<$USER_POOL_ID from step 0>"
CLIENT_ID = "<$USER_POOL_CLIENT_ID from step 0>"
USERNAME = "jim.donachie@cirrushq.com"
PASSWORD = "<the test user's password>"

user = Cognito(USER_POOL_ID, CLIENT_ID, username=USERNAME)
user.authenticate(password=PASSWORD)
print(user.id_token)
EOF
```

Then call the API with the printed token as a Bearer token:

```bash
TOKEN="<id_token printed above>"
curl -s -H "Authorization: Bearer ${TOKEN}" "${READ_API_URL}/rooms/NHHB-2.14"
```

A `200` with the room's current snapshot (matching what step 4 showed)
confirms the Cognito authorizer, the native DynamoDB integration, and the
integration role's trust policy are all correctly wired. If a real user
doesn't exist yet, create one first — `AdminCreateUser` is the only way in,
sign-up is closed:

```bash
aws cognito-idp admin-create-user --user-pool-id "$USER_POOL_ID" --region "$REGION" \
  --username jim.donachie@cirrushq.com \
  --user-attributes Name=email,Value=jim.donachie@cirrushq.com Name=email_verified,Value=true \
  --message-action SUPPRESS
aws cognito-idp admin-set-user-password --user-pool-id "$USER_POOL_ID" --region "$REGION" \
  --username jim.donachie@cirrushq.com --password '<a password meeting the pool policy>' --permanent
```

### One thing worth knowing: Athena access is admin-only right now

Step 7 above only works as a Lake Formation data-lake administrator. An
ordinary IAM principal — even with a full `glue:*` on `*` policy and every
normal Lake Formation SELECT/DESCRIBE grant — currently cannot resolve the
`s3tablescatalog` federated catalog via Athena at all: every attempt fails with
`CATALOG_NOT_FOUND`. This was confirmed directly this session, including
against two different Glue resource-link workarounds (each failed a different
way), so it is a genuine current AWS platform limitation, not a
misconfiguration here. It is why `nested/analytics.yaml`'s QuickSight
flat-table refresh role had to be added to the Lake Formation admin list in
`nested/foundation.yaml` — a PoC-only shortcut, documented there as such.
