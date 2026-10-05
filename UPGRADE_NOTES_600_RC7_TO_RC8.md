# 6.0.0-rc7 -> 6.0.0-rc8

rc8 focuses on two production failures seen in rc7: model transport faults and diaries that merely replay the transcript. It does not change the existing retrieval, time-boundary, body-rhythm, Xinchao state, forgetting, or House data models.

## Model route

- A transport failure such as `Connection error` receives one explicit retry on the same Astr provider.
- The retry is bounded independently and does not inherit a duplicated hidden retry from the shared runtime.
- If the Astr retry still fails, enabled tasks may use the configured external API follow-up once.
- If no follow-up is enabled, or the follow-up also fails, memory generation immediately uses grounded local evidence and records a durable compensation request.
- An exhausted transport route no longer starts another near-identical full provider call through compact recovery.
- Rendering never publishes an ungrounded or failed diary. Raw conversation evidence remains available for later compensation.

## Stable follow-up settings

- External model routing is now stored under the stable plugin data directory instead of the vector database's incidental parent directory.
- Existing `external_models.json`, encrypted model secrets, and `llm_compensation.db` are migrated when the stable destination is empty.
- Migration is copy-only: old files are not removed or overwritten.
- Saving the model page performs a read-back verification and reports whether persistence succeeded.
- Backups include the secret-free external model registry and compensation database. API keys remain excluded.

## Diary formation

- The global diary count hard cap and nightly checkpoint cap default to `3`; all four WebUI presets obey the same ceiling.
- Nightly splitting is driven by represented calendar dates and real gaps of at least two hours. Narrative density can increase processing capacity, but cannot invent extra diary targets.
- Must-write commitments, boundaries, and turning points remain fully covered.
- Supporting evidence is sampled across the scene instead of replaying every turn.
- The rendering prompt requires a scene arc, sensory entry, central emotional movement, and an afterglow rather than one sentence per turn.
- Per-scene soft length targets prevent both skeletal summaries and near-verbatim mega-diaries.
- New gates reject turn-by-turn flow, excessive stage-direction replay, supporting-evidence saturation, excessive density, and overlong single diaries.
- A failed render leaves source material pending; it is not uploaded to Memos as a successful diary.

## Identity core and rolling state

- The legacy profile remains preserved and visible, but its active role is now a compact long-term identity core: stable personality, relationship position, commitments, and boundaries only.
- The identity core injects at most `1000` characters by default. Automatic refresh is no more frequent than every 30 days and requires at least 8 new relevant identity or relationship memories.
- Rolling state cadence is based on distinct meaningful axes rather than compression batch count. The five axes are relationship position, commitments and boundaries, behavior tendencies, recent emotional baseline, and open loops.
- Ordinary changes require 4 distinct axes and a 72-hour minimum interval. Hard commitments, boundaries, and relationship turns can still update immediately.
- The 168-hour maximum wait only applies when meaningful change exists. Repeated daily texture is retired from the state queue without deleting its Episode, diary, or raw source.
- An LLM result that only paraphrases the current state is recorded as a no-op instead of creating another version.
- The main WebUI now exposes both layers on the `人格与状态` page, including current text, eligibility, pending deltas, next update decision, queue suppression counts, and version history.

## Compatibility

- Upgrade directly over rc7. No sync or reindex command is required.
- Existing Memos entries, raw archives, episode links, semantic state, forgetting data, Xinchao state, time insight, and House data are preserved.
- Existing user-selected diary caps remain respected; rc8 changes defaults and presets, not stored user values.

## Verification

- Core modules compile successfully under the AstrBot Python environment.
- Full plugin suite: `695` tests passed.
- Release archive is checked for one top-level plugin directory and excludes caches, logs, runtime databases, and local secrets.
