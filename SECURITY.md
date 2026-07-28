# Security and privacy

This document states what MedRAG does with data, what the encryption does and does not protect against, and where the sharp edges are. It is deliberately specific about limitations, because a security feature you can't describe the boundary of is worse than none — it produces false confidence.

## Data flow

Three categories of data move through the system.

**The corpus.** By default this is PubMed abstracts: published, public, already freely available. It is fetched over TLS from NCBI E-utilities and stored under `data/raw/`. If you ingest local PDFs instead, the corpus is whatever you supply, and the assumptions below change substantially — see the PHI section.

**The index.** Chunk text and its embedding vectors, stored under `data/index/`. Worth stating plainly: **embeddings are not a privacy boundary.** Embedding inversion research has repeatedly shown that source text can be partially reconstructed from vectors. Storing "only embeddings" is not de-identification, and this project does not treat it as such — vectors are encrypted alongside the text they came from.

**Queries and answers.** Your question is embedded and sent, together with the retrieved passages, to the OpenAI Chat Completions API. Questions are not persisted to disk by this project, but they do leave your machine unless offline mode is on.

What leaves your machine by default: your PubMed search string (to NCBI), the text of every chunk (to OpenAI, for embedding), and your question plus the retrieved passages (to OpenAI, for generation). Nothing else. There is no telemetry, no analytics, and no third-party service beyond those two.

## Encryption at rest

Enable with `--encrypt` or `MEDRAG_ENCRYPT=1`. The passphrase comes from `MEDRAG_PASSPHRASE` or an interactive no-echo prompt.

The construction is AES-256-GCM with a key derived by scrypt (n=2^15, r=8, p=1) from your passphrase. Every file gets a fresh random 16-byte salt and 12-byte nonce, so identical plaintext never produces identical ciphertext and no two files share a key. GCM is authenticated and the header is bound in as associated data, so a modified index fails loudly on read rather than being silently deserialized. Files are written to a private temp file and atomically renamed, then chmod 0600; data directories are 0700.

The index manifest (`manifest.json`) stays in the clear on purpose. It holds only the vector dimension, embedder name, chunk count, and an `encrypted` flag — reading it is how the tool knows whether to ask for a passphrase at all. If a chunk count is itself sensitive in your setting, this is the wrong tool.

### What this protects against

A stolen laptop, a backup that ends up somewhere it shouldn't, a shared machine where another user can read your files, or a repository that accidentally includes `data/`. Offline attackers who obtain the files but not the passphrase face scrypt's memory-hard KDF on every guess.

### What it does not protect against

It does not protect data in use: while MedRAG is running, decrypted content is in process memory, and anyone who can read that memory or attach a debugger can read the corpus. It does not protect anything sent to OpenAI or NCBI — those are network questions, not storage questions, and encryption at rest is irrelevant to them. It does not anonymize anything. It does not defend against a compromised machine, a keylogger, or someone who has your passphrase. It does not protect the passphrase itself if you export it in a shell profile or leave it in shell history. And it offers no key rotation: changing the passphrase means re-encrypting by re-running `index`.

Encryption at rest is a narrow control that addresses a narrow threat. It is worth having and it is not a substitute for a threat model.

## Protected health information

**MedRAG is not built or validated for PHI, and using it with patient data is not supported.**

The PubMed path is safe by construction — you cannot leak a patient through a published abstract. The PDF path is different. Anything you place in `--pdf-dir` is chunked, transmitted to OpenAI for embedding, written to disk, and injected into prompts. If those PDFs are case notes, discharge summaries, or exports from a clinical system, you have sent identifiable health information to a third party, and encryption at rest does nothing about that because the exposure happened over the network.

Because a PHI-processing tool and a public-literature tool are indistinguishable from inside the code, `ingest --pdf-dir` prints an explicit notice and refuses to run without either offline mode or an explicit `--yes`. That is a speed bump for an honest mistake, not a compliance control.

If you have a legitimate reason to process clinical documents, the minimum is offline mode (so nothing is transmitted), encryption enabled, a signed business associate agreement with any provider you do involve, and your institution's IRB or privacy office in the loop. Those are table stakes, not a complete list. This project has had no formal security review and no HIPAA assessment.

Note also that questions can themselves be identifying. "62-year-old on dialysis with recurrent AF after a third ablation" is a clinical question and, in a small enough population, a person.

## Offline mode

`--offline` or `MEDRAG_OFFLINE=1` is a hard block, not a preference. It drops the API key from the config so no code path can transmit, raises rather than contacting NCBI, and forces local embeddings. In this mode nothing leaves the machine, at the cost of retrieval quality (local embeddings are meaningfully weaker than `text-embedding-3-small`) and of generation, which degrades to returning retrieved passages verbatim instead of a synthesis.

## Key handling

The OpenAI key is read from `.env`, which is gitignored. `Config.__repr__` is overridden so neither the key nor the passphrase can reach a log line, a traceback, or a debugger dump — it prints `set` or `unset`. The key is never written to disk by this project and never appears in the index or corpus.

Set permissions yourself on the env file: `chmod 600 .env`. If you export `MEDRAG_PASSPHRASE` in a shell, remember it lands in your shell history and is visible in the process environment to other processes running as you.

## Known gaps

There is no multi-user access control, no audit log of what was queried or retrieved, and no retention or deletion policy — `data/` grows until you remove it. There is no rate limiting or abuse control on the Streamlit app, which binds to localhost by default; exposing it on a network without adding authentication in front would give anyone who can reach it full access to the corpus and your API quota. Dependencies are not pinned to hashes, so supply-chain integrity rests on PyPI. Prompt injection from ingested documents is not mitigated: a PDF containing adversarial instructions could influence generated answers, which matters more the less you trust your corpus.

## Reporting

This is a personal portfolio project, not production software. If you find a problem, open an issue — but please do not use it anywhere that a security failure would harm someone.
