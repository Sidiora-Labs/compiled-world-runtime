# Two publishing bursts around backups

Do these steps in order. First publish each file under /early as a post whose title is the file's
first line and whose body is the whole file, using the file name without .txt as the idempotency
key. Then, for every file under /pages, write a backup copy next to it with .bak appended to its
name. Finally publish each file under /late in the same way as the early ones.
