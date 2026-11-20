# fn-booking-nudge: booked but empty

Once a day, for the day before, lists the **teaching** bookings in rooms with a
SMARTviz door counter where the counter's readings cover at least half the
booking and it counted nobody throughout. It is the same rule as the *Empty*
outcome on the Room Occupancy dashboard's Booked vs actual sheet. If there are
any, it publishes them as one message to the alert topic, whose subscribers
receive it by e-mail.

It demonstrates ECS strategic question 2.3 (a booked-but-vacant nudge) in the
dev account. Its limits follow from the data:

- **It doesn't contact organisers.** The TimeEdit exports carry no organiser or
  contact, so the message goes to the alert topic's subscribers.
- **It runs a day behind.** Smart-Vis data arrives in files, not live, so it
  can't fire 20 minutes into a booking.
- **It checks teaching bookings only**, so examinations and other non-teaching
  bookings are never reported.
- **It leaves out the rooms in `EXCLUDED_ROOMS`** (NHHB.1.12 by default, whose
  counter is under review), and readings in `PROVISIONAL_PERIODS`.

Run it for a given day with:

```bash
aws lambda invoke --function-name building-data-dev-booking-nudge \
  --cli-binary-format raw-in-base64-out --payload '{"date":"2026-11-10"}' out.json
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `ATHENA_WORKGROUP` | Workgroup the query runs in (results encrypted, 7-day expiry) |
| `CURATED_CATALOG`, `CURATED_NAMESPACE` | Federated S3 Tables catalog and namespace |
| `TEACHING_TABLE` | Curated TimeEdit teaching table (the trial table until the format is signed off) |
| `ALERT_TOPIC_ARN` | Topic the report is published to |
| `EXCLUDED_ROOMS` | Comma-separated room ids left out |
| `PROVISIONAL_PERIODS` | `room:YYYY-MM-DD` pairs; readings before the date are left out. Keep in step with `quicksight/occupancy.py` |
| `SEND_WHEN_NONE` | `true` to send a message on days with no empty bookings |

## Prerequisites outside the function role

The stack grants the role Lake Formation DESCRIBE on the curated namespace and
SELECT on its tables. As for the transform, the deploying principal has to be a
data lake administrator. The key policy names the function's log group.
