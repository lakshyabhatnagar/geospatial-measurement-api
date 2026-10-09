# Architecture alternatives

## Implemented: Approach A

FastAPI handles a bounded upload synchronously, offloading blocking GDAL/PROJ/SQLite operations to a thread. The original WKB, source CRS, canonical GeoJSON, properties and measured outcomes are persisted in SQLite. Short transactions create a processing record and then atomically publish the complete result set. The service permits one upload and one instance; ordinary reads remain available under WAL.

This choice avoids queue and infrastructure overhead for an internship demonstration. Limitations include client wait time, a cooperative rather than hard native-call timeout, bounded in-memory processing, and no retry after process loss. The core processing functions do not depend on HTTP or database sessions, so the following alternatives can reuse their policy.

## Future Approach B: durable jobs

Use FastAPI, PostgreSQL, Celery, Redis and S3-compatible storage when processing duration or concurrency makes synchronous requests inconvenient. This architecture is documented, not implemented.

1. Validate and durably store the upload. Create the file, job and outbox message in one database transaction. Return `202 Accepted` with the file ID.
2. A dispatcher publishes outbox messages to Redis. An outbox prevents lost work when the database commit succeeds but publication fails. Publication may repeat.
3. A worker receives only a job ID, claims it conditionally in PostgreSQL, and records an attempt number, ownership token and lease expiry. It downloads the object and processes it without holding database locks.
4. Write results to attempt-specific staging rows. In one transaction, verify the ownership token and publish the successful-attempt pointer. Old workers cannot publish after lease replacement.
5. A reconciler re-enqueues expired leases and unclaimed jobs. PostgreSQL remains the status authority.

Start with three attempts, delays of 5/30 seconds, late acknowledgement and prefetch one. Retry transient storage/broker/database failures; do not retry malformed inputs. Use a 150-second soft task limit, 180-second hard limit, five-minute lease and ten-minute Redis visibility timeout. Revisit these values after measurements.

| Failure | Recovery |
|---|---|
| Object upload fails | Return 503; no runnable job |
| Object saved but DB commit fails | Delete best-effort; orphan-object cleanup |
| Broker unavailable | Outbox remains pending |
| Publish succeeds but dispatcher crashes | Duplicate message; conditional job claim |
| Worker lost | Lease expiry and reconciler retry |
| Stale worker resumes | Ownership token rejects publication |
| Commit succeeds but acknowledgement fails | Redelivery observes terminal state and exits |
| Broker loses queued work | Reconciler republishes unclaimed jobs |
| Attempts exhausted | FAILED with diagnostic |
| Cleanup fails | Independent cleanup retry; result remains published |

Add QUEUED/RETRYING states and progress to the existing file endpoint. Retain uploads for 24 hours after terminal processing, then delete separately. Optional idempotency keys map the same key and upload/settings hash to the existing file ID; a changed hash returns 409. PostgreSQL replaces SQLite because API, dispatcher and workers write concurrently.

Reference implementation: [GeoNode importer](https://github.com/GeoNode/geonode-importer). Reference behavior: [Celery tasks](https://docs.celeryq.dev/en/stable/userguide/tasks.html) and [Redis visibility timeout](https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html#visibility-timeout).

## Future Approach C: PostGIS

Choose PostGIS when spatial queries (intersection, containment or proximity) become a requirement. This changes the spatial storage/calculation engine; execution can still be synchronous or queued.

Preserve original WKB and source CRS, store canonical 2D geometry as `geometry(Geometry,4326)`, store attributes/issues as JSONB and add a GiST spatial index. Python resolves source CRS and transforms to WGS84 first. The same per-feature UTM policy chooses the target SRID, then SQL performs:

```sql
SELECT ST_Area(ST_Transform(geometry, :measurement_srid));
SELECT ST_Length(ST_Transform(geometry, :measurement_srid));
```

`ST_SetSRID` labels coordinates already in a CRS; it does not transform coordinates. Geography calculations could be added later as a different method but would not demonstrate the assignment's required projected calculation.

Validate geometry first. Process spatial batches inside savepoints; isolate data-specific failures with individual savepoints. Connection failures, deadlocks and programming errors must reach file/job failure handling. Configure connection health checks, a 30-second statement timeout and a five-second lock timeout. Readiness checks must verify PostGIS and required functions.

Migration requires creating PostgreSQL tables, copying IDs and records, reconstructing canonical geometries, and comparing counts and measurements before cutover. Extra tests must cover SQL isolation, transformation failures and numerical agreement with Approach A. For Approach B, also test duplicate delivery, broker outage, worker kill, stale workers and lost acknowledgements.

References: [TiPG FastAPI/PostGIS API](https://github.com/developmentseed/tipg), [ST_Transform](https://postgis.net/docs/ST_Transform.html), [ST_Area](https://postgis.net/docs/ST_Area.html).
