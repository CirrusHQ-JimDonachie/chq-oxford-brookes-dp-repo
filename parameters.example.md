# Parameters for main.yaml

Copy `parameters.example.json` to `parameters.json`, fill in the values marked `<TODO: set me>`, and check the ARN examples against the account. `parameters.json` is not committed.

| Parameter | Example | Constraint | Notes |
| --- | --- | --- | --- |
| `ProjectSlug` | `building-data` | `^[a-z0-9-]{3,20}$` | Short lowercase name used to build every resource name. |
| `Environment` | `dev` | one of dev, test, prod | Environment this stack deploys; part of every resource name. |
| `DeployRoleArn` | `arn:aws:iam::123456789012:role/cloudformation-deploy` | `^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$` | ARN of the IAM role CloudFormation assumes to deploy this stack. It is named as a Lake Formation data lake administrator so the grants in the engine and analytics stacks can be written. |
| `QuickSightServiceRoleArn` | `arn:aws:iam::123456789012:role/service-role/aws-quicksight-service-role-v0` | `^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$` | ARN of the IAM role QuickSight runs queries as (aws-quicksight-service-role-v0 unless the account uses a custom one). Granted SELECT in Lake Formation and use of the data key for Athena results. |
| `QuickSightPrincipalArn` | `arn:aws:quicksight:eu-west-2:123456789012:user/default/analyst` | `^arn:aws[a-z-]*:quicksight:[a-z0-9-]+:[0-9]{12}:(user|group)/.+$` | ARN of the QuickSight user or group that owns the Athena data source (arn:aws:quicksight:REGION:ACCOUNT:user/NAMESPACE/NAME). |
| `DataProviderPrincipalArn` | `` |  | Optional. ARN of the principal that drops files into the raw prefix from another account. Leave empty when the uploader is in this account and is granted through IAM. |
| `CuratedNamespace` | `building_data` | `^[a-z0-9_]{1,255}$` | Namespace (database) inside the curated table bucket that the mapping configs target. |
| `RawRetentionDays` | `30` | 1 to 3650 | Days a dropped file and its error output stay in the landing bucket before expiry. |
| `ClaimTtlDays` | `14` | 1 to 365 | Days the transform function keeps its record of a processed file version. |
| `ApiEntityType` | `room` | `^[a-z0-9_-]{1,64}$` | Entity type whose current snapshot the read API serves; must match the entity type in the mapping config of the source that feeds it. |
| `AlertEmail` | `<TODO: set me>` | at least 3 characters | E-mail address subscribed to the alert topic. The recipient must confirm the subscription before alerts are delivered. |
| `AlertEventSource` | `building-data.transform` | `^[a-zA-Z0-9._-]{1,256}$` | Source field the transform function stamps on every threshold event, and the value the routing rule matches on. One parameter feeds both so they cannot drift apart. |
| `DataQualityAlarmPeriodSeconds` | `300` | one of 300, 900, 1800, 3600, 21600, 86400 | Window over which rejected records are counted before the data-quality alarm fires. |
| `LogRetentionDays` | `1` | one of the CloudWatch Logs retention values (1 to 3653 days) | Retention for the function and API access log groups. |
| `LogLevel` | `DEBUG` | one of DEBUG, INFO, WARN, ERROR | Log level for the transform function. |

`DeployRoleArn` is the role the Customer's pipeline deploys with; it becomes a Lake Formation administrator. `QuickSightServiceRoleArn` and `QuickSightPrincipalArn` come from the QuickSight account after sign-up. `DataProviderPrincipalArn` stays empty unless files are dropped from another account.
