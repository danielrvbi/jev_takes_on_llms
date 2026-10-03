# Dataset migration

```bash
python -m jev_bench.storage.migrate --dry-run
python -m jev_bench.storage.migrate --apply
```

Dry-run does not change files. Apply acquires existing dataset writer locks,
inventories and stages every source, checks byte hashes and audit/log resolution,
retains originals, then publishes destinations. The journal supports restarting
an interrupted publication. Conflicting destinations and active writers stop
migration. Repeating a completed migration checks destination hashes.
Completed checks ignore transient writer locks and allow local-only quarantine
to be absent in a Git checkout. Published data remains hash-checked, and the
original journal inventories are retained unchanged.

| Previous location | Destination |
|---|---|
| Root benchmark and insurance datasets | `results/experiments/baseline/` |
| `results/jev_30/` | `results/experiments/tev_30/` |
| `results/gemma_30/` | `results/experiments/gemma_30/` |
| `results/jev_api_10/` | `results/experiments/jev_api_30/` |
| `results/jev_api_1/` | `results/experiments/jev_api_1/` |
| `results/prefix_pilot/` | `results/experiments/prefix_pilot/` |
| `hard_case/results/<name>/` | `results/historical/<name>/hard_case/` |
| Contaminated dataset tree | `results/quarantine/contaminated_dont_use/` |

The hosted Jev directory is renamed because it contains 30 repetitions. The
local `jev_30` directory is renamed to identify its actual Tev models.

`results/catalog.json` marks migrated datasets as archived. This status is checked
before runner writes, provider creation, and budget changes. All original data
files, manifests, ledgers, reports, and evidence are preserved byte-for-byte.

`results/migrations/migration.json` retains inventories, progress, and evidence
findings. `relocations.json` maps old absolute prefixes to new locations under
`results/`; mappings also preserve prefixes recorded before repository relocation.
Readers use the map when opening evidence, without changing serialized links or
hashes. Missing historical evidence is reported rather than invented.

Ignored `results/.migration_backup/original/` retains the original tree. It remains
available after migration. Quarantine and backups are excluded from evaluations.
Generated reports go to `results/reports/`; original reports and validation
manifests remain historical records.
