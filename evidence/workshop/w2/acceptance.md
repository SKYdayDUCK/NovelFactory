# W2 addressed mail and recovery

The isolated PostgreSQL 17.11 database upgraded from w001 to w002. Redis 8.0.2 ran locally on loopback. The combined organization, messaging and real Redis integration run passed 13 cases, including W1's seven tests. The first Redis invocation lacked the host's private liblzf loader path and failed with connection refused. The passing run used the supplied Redis distribution's libraries.

```sh
cd workshop
alembic upgrade w002
# Set AW_DATABASE_URL and AW_REDIS_TEST_URL to disposable, isolated services.
python -m pytest tests/test_organization.py tests/test_messaging.py tests/test_notifications.py -q
```

PostgreSQL checks include typed graph edges, denied sender impersonation, authorized forwarding, inaccessible body references, two connections racing for one grant use, old tenures and expired messages, revocation at read/ACK, repeat send and ACK, rate-limited incidents, and recipient cursors. The Redis case claims pending work after the first consumer stops, deletes the isolated test stream, republishes SQL event IDs and confirms the subscriber processes zero duplicate effects. The message log survives loss of Redis. No model calls occur on these paths.

The trusted operator can publish, receive and recover in bounded batches using `python -m agent_workshop.notifications publish|receive|replay` with AW_DATABASE_URL and AW_REDIS_URL. Set independent Redis ACLs for this stream. Only event UUIDs are published. Default mode permits reading durable mail with Redis offline.
