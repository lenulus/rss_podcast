# Lessons Learned: Transcribe Pipeline Tuning

Notes from a debugging session that took an Apple Silicon transcribe pipeline (M2 Ultra, 64 GB, pyannote + Whisper-large-v3 via MLX, ~340-episode backlog) from sub-1× realtime to ~4× the original throughput.

Code delta: ~250 lines across four commits.

## Lessons

### 1. Stay empirical — kill the run, measure, update the model, decide.

The biggest risk wasn't bad code; it was committing too early to a wrong simplification. Each "obvious" optimization that didn't measure first would have shipped, made things equal-or-worse, and obscured the bottleneck.

Concrete failure mode: an early 13× realtime extrapolation came from a single episode that turned out to have a cached diarize. One sample, especially of the special case, is not a baseline.

Why: performance work where you don't trust your hypothesis-of-the-week is the only kind that converges.

### 2. The unit of debugging is "kill and relaunch", not "let it ride".

Multi-hour runs that get progressively slower should be killed. The cost of killing is bounded by what survives in caches. The cost of letting them ride is unbounded.

A 5% probability that "the next episode resets the leak" doesn't justify burning another six hours.

### 3. Stages with different resource profiles need different concurrency.

In this pipeline:
- Pyannote diarize: ~2–5 GB working set, CPU-orchestrated with brief MPS bursts. One instance underutilizes the chip.
- Whisper-large-v3 at batch=48: ~50 GB peak burst per process. Concurrency=2 collides bursts and triggers swap or OOM.

The architectural fix is not "more parallelism." It is "parallelize the small-burst stage, serialize the large-burst stage."

Why: parallelism is for stages where one instance leaves resources idle. "Parallelize everything" is a category error.

### 4. Subprocess isolation is the right reach when in-process reset isn't enough.

PyTorch's MPS allocator and MLX's Metal kernel cache accumulate state across episodes in one Python process. A periodic 5-episode in-process reload only partially clears this. Process teardown reliably clears everything.

Cost: ~30–60s model-load overhead per episode. For a 100-episode backlog, that's ~1 hour — much less than a leaky run's accumulated tax.

Why: some state lives below Python's heap. The only reliable way to free it is to exit the process.

### 5. Cache files per stage are free progress preservation across kills.

`.diarize/<stem>.json` per episode, `.chunks/<stem>/chunk_NN.json` per chunk. Completed stages survive kills. The next run picks up where the previous one left off.

This invariant made the many kill-and-relaunch cycles cheap. Without it, every restart would have been a hard fork.

Why: pipelines that materialize intermediate state survive operator interruption. Pipelines that hold everything in memory and write only at the end punish operators who don't get lucky.

### 6. Tunable knobs as a portability strategy.

Two knobs do all the work across hardware:
- `subprocess_concurrency`: scales with diarize memory headroom.
- `whisper_batch_size`: scales with peak Whisper memory ceiling.

M1 Pro 32 GB and M2 Ultra 64 GB run the same code with different values for those two. No hardware-specific code paths.

Why: hardware constants in code rot the moment someone runs on a different chip. Knobs survive.

### 7. Read the right observability layer.

Reading log lines hides utilization questions. Activity Monitor (or `top`, or `powermetrics`) tells you whether the chip is actually busy.

Key inflection: noticing CPU at ~10% during serial subprocess-per-episode, vs ~30% during conc=10 diarize-only, was what made the design's headroom obvious.

Why: throughput is one signal; utilization is a different signal. Both are needed to know whether you're chip-bound or design-bound.

### 8. Don't trust line-buffered stdout when subprocesses pipe to a parent.

Python's `print` is line-buffered on a TTY but block-buffered when stdout is a pipe. Child processes whose output is captured by a parent will look hung for minutes while output sits in a 4 KB buffer.

Fix: pass `-u` to the child interpreter. Also flush any custom stdout writer (e.g., a prefixing wrapper for concurrent worker labels) on each logical line.

Why: under concurrency, worker silence is indistinguishable from worker hang. Silence is not success.

### 9. Precedence pattern: CLI > TOML > default.

Consistent across `--diarize`, `--subprocess-per-episode`, `--subprocess-concurrency`, `--model`. CLI flags are per-invocation overrides; TOML is per-feed defaults; code constants are fallbacks.

Why: operators need to override behavior without editing config files. Pick the pattern and stay in it.

### 10. The work is mostly diagnosis, not coding.

The four commits total ~250 lines. The session was 90% measurement, hypothesis-testing, and tradeoff framing. The code followed naturally once the right question was identified.

A pure SWE framing — "I need a `--diarize-only` flag, here's the PR" — would have missed the point. Figuring out *that* flag was the right thing to build, and *why*, was the work.

## Anti-patterns we avoided

- **"Just buy a bigger machine."** Would have masked the design problem; the M2 Ultra was already underutilized.
- **"Use a different library."** WhisperX uses pyannote underneath; NeMo is CUDA-only. Same problems plus new bugs.
- **"Parallelize everything to concurrency=N."** Whisper concurrency=2 hits OOM on 64 GB hardware.
- **"Set whisper_batch_size higher to use the GPU more."** Bigger batches mean bigger memory bursts — the opposite of what's safe.
- **"Kill the run and start over from scratch."** Cache files preserved most of the work across each kill cycle.

## Reference: measured numbers (M2 Ultra, latent-space feed)

| Phase / mode | Per-episode | Aggregate ratio |
|---|---|---|
| Pyannote diarize alone, concurrency=1 | ~150–200s for 60 min audio | ~5–6× realtime |
| Pyannote diarize alone, concurrency=10 | ~500s for 60 min audio | ~48× aggregate (~4.8× per worker) |
| Whisper-large-v3 batch=48, no diarize contention | ~100–140s for 60 min audio | ~25–50× realtime |
| Mixed pipeline concurrency=1 (cumulative across many eps) | — | ~12× realtime |
| Mixed pipeline concurrency=2 | — | UNSAFE — swap thrashing, killed |
| Two-pass (diarize@10, then transcribe@1) | — | ~9 hours projected for the full 220 h backlog |

## When to revisit

- Hardware changes: re-tune `subprocess_concurrency` and `whisper_batch_size`.
- If pyannote ships an MLX-native backend or an alternative MLX-based diarize library appears, the asymmetric concurrency story may shrink.
- If a feed's speaker-count distribution shifts (e.g., adding multi-host panel shows like the NeurIPS recaps), expect diarize-time variance to widen.

## Related code

- `ss.py:should_subprocess_per_episode` — TOML/CLI resolver for the subprocess-mode flag.
- `ss.py:subprocess_concurrency_for` — resolver for the concurrency knob.
- `ss.py:_run_feed_via_subprocess` — ThreadPoolExecutor-based parent loop with `--only` per-worker assignment.
- `ss.py:_PrefixedWriter` — line-buffered stdout wrapper for distinguishing concurrent worker output.
- `ss.py:run_transcribe` — top-level dispatch; honors `--diarize-only` to skip Whisper.

Commits:
- `c404e5e` — Roll tiktoken forward to unblock setup on Python 3.13
- `d63979d` — Add subprocess-per-episode mode for leak-resistant transcribes
- `c08c388` — Add subprocess_concurrency for parallel transcribe workers
- `ce925b1` — Add --diarize-only mode for high-concurrency pre-diarize pass
