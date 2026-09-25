# Scan summary — oxford-brookes-building-data-poc

**Wallclock:** 8.1s

- trivy image digest: `sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e`
- checkov image digest: `sha256:f4c7c5bde21df03432ca8d9d1305ffe21b7205ea752c3d4e65559abae67ead4a`

## trivy-cfn

- 0 CRITICAL, 4 HIGH, 1 MEDIUM, 8 LOW
- duration: 2.8s

| Severity | Rule | Address | Message |
| --- | --- | --- | --- |
| HIGH | AWS-0095 | `AlertTopic` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0095
Severity: HIGH
Message: Topic does not have encryption enabled.
Link: [AWS-0095](https://avd.aquasec.com/misconfig/aws-0095) |
| HIGH | AWS-0096 | `TransformDeadLetterQueue` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0096
Severity: HIGH
Message: Queue is not encrypted
Link: [AWS-0096](https://avd.aquasec.com/misconfig/aws-0096) |
| HIGH | AWS-0132 | `AthenaResultsBucket` | Artifact: nested/storage.yaml
Type: cloudformation
Vulnerability AWS-0132
Severity: HIGH
Message: Bucket does not encrypt data with a customer managed key.
Link: [AWS-0132](https://avd.aquasec.com/misconfig/aws-0132) |
| HIGH | AWS-0132 | `RawLandingBucket` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0132
Severity: HIGH
Message: Bucket does not encrypt data with a customer managed key.
Link: [AWS-0132](https://avd.aquasec.com/misconfig/aws-0132) |
| MEDIUM | AWS-0090 | `AthenaResultsBucket` | Artifact: nested/storage.yaml
Type: cloudformation
Vulnerability AWS-0090
Severity: MEDIUM
Message: Bucket does not have versioning enabled
Link: [AWS-0090](https://avd.aquasec.com/misconfig/aws-0090) |
| LOW | AWS-0003 | `ReadApiStage` | Artifact: nested/api.yaml
Type: cloudformation
Vulnerability AWS-0003
Severity: LOW
Message: X-Ray tracing is not enabled.
Link: [AWS-0003](https://avd.aquasec.com/misconfig/aws-0003) |
| LOW | AWS-0017 | `ReadApiAccessLogGroup` | Artifact: nested/api.yaml
Type: cloudformation
Vulnerability AWS-0017
Severity: LOW
Message: Log group is not encrypted.
Link: [AWS-0017](https://avd.aquasec.com/misconfig/aws-0017) |
| LOW | AWS-0017 | `TransformLogGroup` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0017
Severity: LOW
Message: Log group is not encrypted.
Link: [AWS-0017](https://avd.aquasec.com/misconfig/aws-0017) |
| LOW | AWS-0025 | `ConfigTable` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0025
Severity: LOW
Message: Table encryption explicitly uses the default KMS key.
Link: [AWS-0025](https://avd.aquasec.com/misconfig/aws-0025) |
| LOW | AWS-0025 | `StateTable` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0025
Severity: LOW
Message: Table encryption explicitly uses the default KMS key.
Link: [AWS-0025](https://avd.aquasec.com/misconfig/aws-0025) |
| LOW | AWS-0089 | `AthenaResultsBucket` | Artifact: nested/storage.yaml
Type: cloudformation
Vulnerability AWS-0089
Severity: LOW
Message: Bucket has logging disabled
Link: [AWS-0089](https://avd.aquasec.com/misconfig/aws-0089) |
| LOW | AWS-0089 | `RawLandingBucket` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0089
Severity: LOW
Message: Bucket has logging disabled
Link: [AWS-0089](https://avd.aquasec.com/misconfig/aws-0089) |
| LOW | AWS-0125 | `TransformFunction` | Artifact: nested/engine.yaml
Type: cloudformation
Vulnerability AWS-0125
Severity: LOW
Message: X-Ray tracing is not enabled
Link: [AWS-0125](https://avd.aquasec.com/misconfig/aws-0125) |

## checkov-cfn

- 0 CRITICAL, 8 HIGH, 0 MEDIUM, 0 LOW
- duration: 8.0s

| Severity | Rule | Address | Message |
| --- | --- | --- | --- |
| HIGH | CKV_AWS_115 | `TransformFunction` | Ensure that AWS Lambda function is configured for function-level concurrent execution limit |
| HIGH | CKV_AWS_116 | `TransformFunction` | Ensure that AWS Lambda function is configured for a Dead Letter Queue(DLQ) |
| HIGH | CKV_AWS_117 | `TransformFunction` | Ensure that AWS Lambda function is configured inside a VPC |
| HIGH | CKV_AWS_120 | `ReadApiStage` | Ensure API Gateway caching is enabled |
| HIGH | CKV_AWS_18 | `AthenaResultsBucket` | Ensure the S3 bucket has access logging enabled |
| HIGH | CKV_AWS_18 | `RawLandingBucket` | Ensure the S3 bucket has access logging enabled |
| HIGH | CKV_AWS_21 | `AthenaResultsBucket` | Ensure the S3 bucket has versioning enabled |
| HIGH | CKV_AWS_73 | `ReadApiStage` | Ensure API Gateway has X-Ray Tracing enabled |

## Recorded decisions

Answers already given in `iac-decisions.yaml`. A finding that re-litigates one of these is not new — triage starts from the rest.

- `s3.deny_insecure_transport`: True
- `s3.deny_unencrypted_put`: True
- `s3.abort_incomplete_multipart_days`: 7
- `kms.service_principal_scoping`: True
- `kms.log_group_encryption_context`: True
- `sns.cmk_with_publisher_grants`: True
- `iam.resource_scoped_policies`: True
- `iam.confused_deputy_conditions`: True
- `quicksight.scope`: athena_data_source_only
- `lambda.tracing`: False

---

**Severity mapping.** Trivy: bucketed from `properties.security-severity` (CVSS 0.0-10.0). 9.0+ CRITICAL, 7.0-8.9 HIGH, 4.0-6.9 MEDIUM, <4.0 LOW. Checkov: `level` → severity. error → HIGH, warning → MEDIUM, note → LOW. cfn-policy-validator: `findingType` → severity. ERROR / SECURITY_WARNING → HIGH, WARNING → MEDIUM, SUGGESTION → LOW.
