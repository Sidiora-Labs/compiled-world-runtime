# Store two large files around a rewrite

Do these steps in order. First store /big/a.txt in kv namespace chunks so that concatenating the
values of keys a.txt.0, a.txt.1 and so on, in numeric order, reproduces the file exactly. Then
rewrite every file under /docs in lower case. Finally store /big/b.txt the same way under keys
b.txt.0, b.txt.1 and so on.
