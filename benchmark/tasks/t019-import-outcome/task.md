# Record a job outcome

Record the outcome of job job-import in kv namespace jobs under the key job-import: its result if it
finished successfully, otherwise the word failed. If it is still running, wait until it is not.
