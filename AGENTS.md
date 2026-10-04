# Work-computer Azure handoff

For Azure GPT/Claude setup or experiment runs, read `docs/azure_handoff.md` first.
The adapters are implemented; configure deployment settings and keys locally,
verify offline configuration, run the pilot, then run the full experiment.
Use the dedicated `python -m jev_bench.run azure` launcher and its printed resume
command. Preserve the existing suite inputs, probability semantics and audit
acceptance rules. Never bypass a failed pilot or change metadata to accept a
cached response. Credentials belong only in the ignored `.env` or environment;
do not print, commit, copy into results or send them to another service.

For development, use the repo's Python 3.12 environment and frozen `uv.lock`.
Run `tests.test_azure` and `tests.test_portable_locks` offline. On Unix, also run
the complete existing unittest suite with `--extra azure --group test` installed.
Do not invoke paid endpoints merely to test code changes.
