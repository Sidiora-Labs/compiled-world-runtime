# Purge a queue around a namespace move

Do these steps in order. First remove /queue/stale-1.job and /queue/stale-2.job. Then move every key
of kv namespace jobs-v1 into namespace jobs-v2 with the same key and value, leaving jobs-v1 empty.
Finally remove /queue/stale-3.job and /queue/stale-4.job. /queue/active.job must stay.
