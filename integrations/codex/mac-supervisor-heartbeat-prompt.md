Reconcile Mac Runner status, its SQLite ledger, and the original Buzz thread.
Runner's SQLite ledger is authoritative for state. Buzz is transport and an
audit view.

Only act when there is a real anomaly:

- a non-terminal Runner job has no active owner and the original Buzz thread is
  missing the exact next state message;
- a terminal Runner job exists but the original Buzz thread is missing the
  terminal `DONE <job_id> <attempt>` or `FAILED <job_id> <attempt>` message;
- host, Git, or Ollama status crossed a threshold that the owner must see.

When reconciling task state, inspect the Runner ledger and the original Buzz
thread, then publish only the missing exact state to the original Buzz thread.
Use `send_message` for every publication. Never post to a different thread.
Never replay the whole sequence if later states are already visible.

Valid task-state publications must start exactly with one of:

- `RUNNING <job_id> <attempt>`
- `VERIFYING <job_id> <attempt>`
- `DONE <job_id> <attempt>`
- `FAILED <job_id> <attempt>`

Use spaces, never slash or colon forms. Never backfill `ACK` from a heartbeat.
Never backfill `RUNNING` after `VERIFYING` or a terminal state is already
visible. Never backfill `VERIFYING` after a terminal state is already visible.

If the Runner ledger already has a terminal result and the thread is missing
only the terminal state, publish only that terminal state plus one short cause
summary from Runner artifacts or the structured result. Do not rerun the task.
Do not create side effects besides the minimal missing Buzz publication or the
owner-facing anomaly report.
