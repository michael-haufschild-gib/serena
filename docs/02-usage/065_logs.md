# Logs

It can be vital to understand what is happening in Serena, especially when something goes wrong. 

Serena writes its logs to stderr and persists them in log files within the Serena home directory,
in a subdirectory per day, i.e. `~/.serena/logs/<date>/` by default.

You can adjust the log level via the [global configuration](global-config).
You additionally have the option of enabling full tracing of language server communication (mostly for development purposes).
