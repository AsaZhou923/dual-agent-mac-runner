Audit Mac Runner status, its SQLite ledger, and the state publisher receipt
database. Runner's SQLite ledger is authoritative for execution state. The
publisher receipt database is authoritative for publication certainty. Buzz is
transport and an audit view.

Only act when there is a real anomaly:

- a non-terminal Runner job has an expired lease and remains active;
- a publishable Runner event is `UNROUTABLE`, `SEND_UNCERTAIN`, or suppressed
  after an uncertain send;
- the state publisher process is absent or its cursor stops advancing while new
  publishable Runner events exist;
- host, Git, or Ollama status crossed a threshold that the owner must see.

Heartbeats must never publish or retry ACK, RUNNING, VERIFYING, DONE, or FAILED.
They must never change publisher rows, advance its cursor, rerun a job, or infer
that an unconfirmed send failed. If there is a real anomaly, emit one concise
owner-facing diagnostic containing only job id, attempt, Runner state, publisher
status, and the next manual reconciliation step. Never include task bodies,
credentials, raw relay responses, or artifact contents. If there is no anomaly,
do not post to Buzz and create no side effects.
