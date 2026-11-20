"""Fixtures for the booked-but-empty report's tests.

The path shim and the environment come first: the handler module imports by
bare name from app/, as it does once packaged, and reads its environment and
creates its clients at import time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).parent / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

os.environ.update(
    {
        "ATHENA_WORKGROUP": "obu-poc",
        "CURATED_CATALOG": "s3tablescatalog/obu-curated",
        "CURATED_NAMESPACE": "building_data",
        "TEACHING_TABLE": "trial_timetable_teaching",
        "ALERT_TOPIC_ARN": "arn:aws:sns:eu-west-2:123456789012:obu-poc-alerts",
        "EXCLUDED_ROOMS": "NHHB.1.12",
        "PROVISIONAL_PERIODS": "NHHB.1.15:2026-10-14",
        "AWS_REGION": "eu-west-2",
        "AWS_DEFAULT_REGION": "eu-west-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }
)
