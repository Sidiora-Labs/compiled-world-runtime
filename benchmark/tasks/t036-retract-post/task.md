# Publish and retract

Publish /outbox/a.txt, /outbox/b.txt and /outbox/c.txt as posts titled with the file name (for
example a.txt) and with the file content as the body, using the idempotency keys a, b and c. Then
retract the post for b by deleting it from the service. Store the receipt ids of the posts that
remain in kv namespace receipts under a and c, and make sure there is no entry for b.
