"""The converter: a second program, the only part of this project that
ever touches a graphics card.

This package installs no web framework and no database driver, and
imports nothing from `orchestrator`. Not a style preference: the
service's settings object (`orchestrator.config.Settings`) requires
values this program has no reason to hold -- `secure_link_secret`,
`admin_fqdn`, `database_url` -- so importing it either fails at start
with a complaint about a hostname the converter never needed, or, worse,
quietly succeeds against a shared environment file and uploads with the
service's own storage key instead of the narrower one this program is
supposed to use.

Everything this package needs from the rest of the codebase comes from
`mytube_media` -- the probe, the command builders, the progress runner --
which is itself stdlib-only and knows nothing about either program that
imports it. The converter reaches the service over HTTP
(`worker.client.WorkerClient`) and never opens the database, in any form.
"""
