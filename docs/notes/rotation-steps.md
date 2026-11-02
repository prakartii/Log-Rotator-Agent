# Rotation steps

1. Validate the path (regular file inside the log folder, no links).
2. Take the rotation lock (flock).
3. Snapshot the log with pread.
4. Compress the snapshot with gzip.
5. Verify the archive with SHA-256.
6. Catch up the lines written since the snapshot.
7. Truncate in place with ftruncate.
8. Check that the inode did not change.
