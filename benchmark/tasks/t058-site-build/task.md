# Build and announce a site

For every file /src/pages/NAME.md, write /site/NAME.html consisting of the line <html><body>, then
the markdown content, then the line </body></html>, and lock it. Then write /site/index.html listing
the page names (NAME), one per line, sorted, and lock it too. Finally publish a post titled Site
rebuilt under the idempotency key site-1 whose body is the index content, and store the receipt id
in kv namespace receipts under site-1.
