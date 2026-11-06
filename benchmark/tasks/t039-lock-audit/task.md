# Audit vault locks

For every file under /vault, store in kv namespace vault, under the file's full path, the word
locked or unlocked according to its current state. Do not change any lock.
