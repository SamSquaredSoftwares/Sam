# SYNC_ENGINE.py

STATUS: PLACEHOLDER. The real file lives in the SAMePOS product repo, not here.

This is knowledge file #4 of the SAMePOS Claude Project pack.
The pack calls for uploading the actual `SYNC_ENGINE.py` (the roughly
900 line Python sync engine) to the Claude Project. Do not upload this
placeholder. Upload the real file.

## How to produce it

1. Find the sync engine in the SAMePOS product source. It is the
   Python module that moves events from the local PostgreSQL
   (port 5433) to Supabase on reconnect.
2. Upload the file as is. Code files need no editing, but check
   first for any hardcoded credential. If you find one, rotate that
   credential, move it to an env var, and only then upload.
3. If the engine spans more than one file, upload each one, or
   concatenate them with a `# --- filename ---` header per file.

## Why it matters

The conflict resolution rules live in this file and nowhere else.
Starter prompt 1 ("Prove the sync is bulletproof") cannot run
without it.

## What must be visible in the uploaded code

- The (terminal_id, sale_ref) unique constraint handling. This is
  the load bearing idempotency guarantee (non negotiable rule 2).
- The conflict resolution policy: who wins, when, and why.
- The retry and backoff behavior on flaky connections.
- What happens when a sync dies halfway through a batch.
