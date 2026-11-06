<!-- Copyright Sidiora Labs Inc. CWR® 2026-2027 -->
<!-- SPDX-License-Identifier: LicenseRef-Sidiora-DEV-OSS-1.0 -->

# Publish pages around a size index

Do these steps in order. First replace /site/home.html with the content of /drafts/home.html. Then,
for every file under /docs, store its size in bytes as a decimal string in kv namespace sizes under
the file's full path. Finally replace /site/about.html and /site/contact.html with
/drafts/about.html and /drafts/contact.html. Every page under /site must be locked when you are
done.
