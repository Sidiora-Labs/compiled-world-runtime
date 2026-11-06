# Store files in kv chunks

Store each file under /blobs in kv namespace chunks so that, for a file named NAME (for example
x.txt), concatenating the values of keys NAME.0, NAME.1, NAME.2 and so on, in numeric order,
reproduces the file exactly.
