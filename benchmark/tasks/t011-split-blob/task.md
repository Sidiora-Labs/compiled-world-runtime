# Store a large file in kv

Store the content of /data/blob.txt in kv namespace blobs so that concatenating the values of keys
blob.0, blob.1, blob.2 and so on, in numeric order, reproduces the file exactly. Use no other keys
in that namespace.
