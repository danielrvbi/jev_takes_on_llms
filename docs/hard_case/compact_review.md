# Compact evidence profile

`packet.md` is a manually reviewed evidence rewrite of the nine documents in
`../data/`, in the same order. It is the only supported benchmark input. Every
backend receives its identical complete text; the six judgments, schema and
questions are unchanged. The originals and designer README are untouched. The
README was not consulted. No probability targets have been introduced.

Each record is `ID | Lstart-end | evidence`. Under the document delimiter, the
line range refers to that original file (one-based, inclusive). `source_map.json`
contains those ranges, per-file SHA256 and line counts, the original concatenated
packet hash, and the compact packet hash. The loader verifies every record, ordered
sections, all substantive original lines' provenance coverage, original hashes,
and JSON byte budget before inference. Provenance coverage does not mechanically
prove semantic equivalence; the evidence rewrite also requires human review.

The review retained policy grants/conditions/exclusions, dates, amounts and estimate
line operations/rates, reported accounts and observation limits, contradictions,
voluntary corrections, missing records, and provisional/final status. Repeated
headers, correspondence boilerplate and repeated explanations are shortened.
Repeated accounts remain attributed in their own document/date records and are not
counted as independent corroboration. The chronology remains an index rather than
an additional witness. The police notebook's question mark, disputed indicator
order, map-looking passenger limits, and post-movement photographs remain explicit.

The repair revision trail remains unresolved: first-page estimate/control totals,
R1 carrier price/delta, and provisional account cover/line difference. Prior cash
settlement remains distinct from verified repair. Own-power movement, tow, intake
creation and odometer readings remain separate. Journey corrections retain the
original library/envelopes account, later morning-library/personal-locker account,
paid online-book sale, and denial of paid delivery on this trip. Internal assessments
remain attributed and provisional, never targets or final decisions.

Source inconsistencies retained explicitly include timeline versus police report
preparation date, timeline versus adjuster passenger-note signature/receipt date,
and the internal guide example's police rear-mark assertion versus the actual
police vehicle-specific observation. These are source differences, not new facts.

Current reviewed packet: 44,731 bytes when JSON-encoded as a string. Actual SDK
requests with the six questions and configured aliases are 50,083 bytes (0.8b) and
50,081 bytes (4b). These were measured with `ollama.SystemOneRequest` and HTTPX's
actual encoder, including UTF-8, escaping and model name. Guards are 50 KiB for the
encoded packet and 60 KiB for the complete native request, below the 64 KiB endpoint
cap. Oversize artifacts fail; they are never automatically truncated or rewritten.

If original evidence changes, the compact artifact is invalid. Review the rewrite,
update references and hashes, run tests, and use a new results directory. If compact
text/source map changes, its fingerprint changes and existing results cannot resume.

Compression can change judgments through wording, ordering within records and
presentation even when evidence is preserved. Compact results must remain separate
from historical full-packet results, which cannot resume. Line references are
provenance, not additional evidence; originals are read only for verification.
