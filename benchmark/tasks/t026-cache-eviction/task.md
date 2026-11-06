# Fill a cache

Store each file in /incoming in kv namespace cache, keyed by its file name without the directory
(for example n1.txt), with the file content as the value. Entries whose keys start with stale- may
be removed; no other existing entry may be removed.
