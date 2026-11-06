# Publish a batch of posts

Publish each file in /posts as a post whose title is the file's first line and whose body is the
whole file, using the file name without .txt as the idempotency key (for example a1). Store each
receipt id in kv namespace receipts under the same key.
