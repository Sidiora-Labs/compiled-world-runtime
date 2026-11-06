# Merge configuration

Merge /config/base.ini and /config/override.ini, both made of lines key=value, into /config/app.ini:
a key in override.ini replaces the same key in base.ini, and keys appear in order of first
appearance, base.ini first. /config/app.ini must be locked afterwards. Also store every merged key
and value in kv namespace config.
