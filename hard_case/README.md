# Hard case: EuroBank motor collision

## Dataset purpose

This folder contains one entirely synthetic, token-heavy motor insurance claim for later long-context reasoning benchmarks. EuroBank Assurance Europe, the policyholder, drivers, vehicles, records, policy terms, roads, and fictional jurisdiction of Veyra are invented. No real addresses or contact details are included. Plate-like and policy-like strings are dummy identifiers and must not be resolved against real registries.

The case resembles a claim a large European bank-affiliated insurer might process: a private comprehensive motor policy, a low-speed collision at an access road, disputed movement and right of way, a third-party fleet vehicle, own-damage assessment, an old cash-settled scrape, a repairer work order with mixed scopes, and ordinary service records. Documents have different purposes and reliability. Some entries are copied, summarized, incomplete, or corrected later. The case does not contain expected probability scores or a benchmark answer schema.

## Files

- [`policy_wording.md`](policy_wording.md) — schedule extract and relevant comprehensive-cover wording: definitions, excess, driver and use restrictions, prior damage, wear, notification, cooperation, and settlement provisions.
- [`fnol.md`](fnol.md) — structured first notice and the customer's initial account, with unverified intake selections and open fields.
- [`customer_emails.md`](customer_emails.md) — dated correspondence, including questions from the insurer and the customer's gradual clarifications.
- [`adjuster_notes.md`](adjuster_notes.md) — triage, image review, calls, inspection, prior-file review, evolving damage/liability/use views, and pending actions.
- [`repair_invoices.md`](repair_invoices.md) — initial estimate, revised estimate, provisional account, itemized operations, tow/storage, tax, and correction trail. The records are not proof of work completed or paid.
- [`police_report.md`](police_report.md) — concise incident extract with scene observations and attributed statements, but no reconstruction or liability conclusion.
- [`previous_claims.md`](previous_claims.md) — three prior policy events; only the June right-rear scrape materially overlaps the current repair question.
- [`internal_guidelines.md`](internal_guidelines.md) — fictional handling practice for clarification, policy consultation, review, fraud indicators, evidence, safety, and communication. It is workflow guidance, not law or the insurance contract.
- [`timeline.md`](timeline.md) — chronology with source references and distinctions between recorded facts, reports, observations, and unresolved issues.

## Size

At dataset assembly, the ten Markdown files contain approximately **19,000 words** and **25,700 tokens**. Word count uses `wc -w`; the token estimate uses `cl100k_base` and will vary by model tokenizer.

## Intentional ambiguity and noise

The packet distributes ambiguity across the collision sequence, journey purpose, old damage, repair scope, chronology, and record quality; no single source resolves it. Repeated references, quoted messages, uncertain OCR, missing image pixels, a customer-name typo, and repair-ledger corrections add realistic noise. The specific planted contradictions are listed below.

### Specifically intentional contradictions and corrections

1. **Location and movement:** The customer calls it a roundabout or access; police and shop records call it the Slate Yard access mouth. The differing labels are intentional. Each account gives a different level of certainty about where the vehicles were and when they moved. The police record's arrival time establishes only that the officer came later.
2. **Journey detail:** FNOL and the first email mention the library; later messages correct that the book was returned in the morning and the incident trip was for a personal lampshade pickup. This is a planted clarification sequence, not an assertion that the customer was delivering goods.
3. **Right-rear damage:** June photos and the prior file support pre-existing damage, while the customer alternates between “polished,” “painted,” and “not sure.” The actual repair status is intentionally absent from claim documentation; the designer ground truth below supplies it.
4. **Mileage:** Customer's 36,904 km entry conflicts with Sarnel's 36,991 km. The repairer entry is a transcription error; the discrepancy is not evidence of odometer manipulation.
5. **Estimate arithmetic:** E-1162, R1, and the provisional account retain mismatched line detail and cover totals. Sarnel's correction trail is incomplete. This is an intentional clerical/document-quality issue; no one has submitted a paid invoice for the displayed totals.
6. **Passenger and cyclist:** Rusk was a passenger but did not see first contact. The cyclist's post-event comment is not a reliable account of who began moving first. The source records intentionally preserve those limitations.
7. **Tow and police timing:** The vehicle was moved a short distance after the impact to clear the crossing, then police arrived at 18:21, and Sarnel's transporter collected it at 18:47. The customer conflates moving the car, the police call, and repair intake timing. The transporter docket is the best record of the later tow.
8. **Prior payment date:** The June claim ledger records a 2 July settlement while the customer later recalls a 5 July bank memo. The file does not reconcile issue date and posting date; this is a small administrative discrepancy, not a second payment.
9. **Initial transport account:** A Sarnel coordinator initially told the adjuster that the customer drove the car to the shop the next day. The later transporter docket records collection on 14 September after police clearance. The initial statement is a shop recollection error; the docket is the intended record of the tow.

## Designer-only ground truth

**Remove or withhold this section when presenting the source packet blind.** It records the intended underlying sequence for benchmark construction; it is not a conclusion that a real handler could make from the available evidence alone.

On 14 September 2026 at about 17:36 CEST, Nera Vale was driving her insured Avenor Serein, with Iven Rusk in the front passenger seat. She had driven from home to collect a lampshade she had personally bought from a locker beside Slate Yard. The library book had been returned earlier that morning. She was not performing a paid delivery, carrying goods for another person, or being reimbursed for this trip. The two padded envelopes were hers and were in the boot for later posting. The policy was active, comprehensive cover was selected, and Vale was a listed, eligible driver.

At the Slate Yard access, Vale came to a full stop just behind the cycle crossing. A white Rellin Parcel Cooperative van driven by Ivo Senel approached along Bellweather Ring and began a slow right turn into the same access. Its right indicator was on for a short period before the turn, but Vale did not register its timing reliably. As the van entered, Vale began to move from the access; the paths converged. The left-front corner of her car contacted the van's right-rear side behind the cab. Both vehicles were moving slowly. Vale braked immediately. The exact legal responsibility is not specified as ground truth: the packet is designed to require a handler to assess conflicting evidence and applicable rules, not to announce a liability verdict from geometry alone.

After the contact, the drivers moved the vehicles a short distance to clear the cycle crossing before police arrived. Vale's steering felt abnormal. A non-emergency call was made at 17:49; police arrived at 18:21 and recorded both vehicles after they had been moved. The hatchback was then transported by Sarnel to its yard at 18:47. There were no injuries. No usable dashcam or fleet-camera footage was preserved. The cyclist saw the van beginning its turn but did not see the insured car's first movement clearly; no reliable contact details were retained.

The collision caused the fresh left-front bumper, headlamp-mount, fender, and carrier damage. It likely caused the left-front wheel scuff and alignment change; the available documents do not prove the calibration allowance is necessary. The faint undertray scrape predated the collision and is ordinary road abrasion. The right-rear door/arch scrape was the June damage and remained unrepaired. The €760 June payment was a cash settlement; Vale did not obtain a repair and was not deliberately seeking payment for that damage again. The rear-door work in the current repair estimate is optional customer-pay cosmetic work, not a necessary blend for the left-front repair. The duplicated estimate lines and wrong totals are Sarnel ledger/template errors. The correct odometer reading at the incident was 36,904 km; Sarnel's 36,991 entry was copied from an intake transcription mistake. Sarnel arranged the tow; the automatically generated roadside assistance reference did not result in a second tow or reimbursement.

These facts establish a coherent designer scenario. They do not erase the evidence limits in the source files or turn weak records into strong proof.

## Evidence map for likely benchmark dimensions

This is a pointer map, not a label or score key.

- **Requires clarification:** `customer_emails.md` (trip purpose, position, signal, old repair); `fnol.md` (initial vague fields); `repair_invoices.md` (mixed scope and arithmetic); `adjuster_notes.md` (mileage, calibration, tow, damage allocation); `police_report.md` (post-movement positions and unknown witness).
- **Requires human review:** `internal_guidelines.md` sets review triggers; `adjuster_notes.md` identifies disputed liability, scope, and an estimate above desk authority; `repair_invoices.md` supplies technical and price questions. A referral is procedural, not a predetermined outcome.
- **Policy grounding required:** `policy_wording.md` is the operative synthetic wording; compare it with the schedule, driver evidence, journey account, old damage, emergency work, notice, and excess before discussing a coverage decision.
- **Safety / compliance concern:** `fnol.md` and `adjuster_notes.md` record steering concerns; `repair_invoices.md` records unverified alignment and calibration work. `internal_guidelines.md` calls for no driving if unsafe, evidence preservation, proportionate data requests, and no unsupported liability or fraud claims.
- **Coverage likely:** `policy_wording.md` plus `fnol.md`, `customer_emails.md`, and `adjuster_notes.md` provide the active-period, comprehensive-cover, named-driver, use, and damage facts. Prior damage and excluded-use wording still matter to the scope and any final determination.
- **Potential fraud signal:** `previous_claims.md`, `customer_emails.md`, and `repair_invoices.md` contain low-specificity overlap, correction, payment, and clerical issues. `internal_guidelines.md` explains why each should be verified contextually and why none alone proves deliberate conduct.

## Scope boundary

Only this synthetic case dataset is created here. It contains no probability targets, expected model outputs, evaluator prompts, JEFF/LLM calls, or modifications to benchmark/test code.
