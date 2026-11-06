# Rotate logs

Rotate the logs app and db under /logs. For each of them the .3 file is discarded, .2 becomes .3, .1
becomes .2, the live log (for example /logs/app.log) becomes .1, and a new empty live log is
created. Each new live log must be locked when you are done.
