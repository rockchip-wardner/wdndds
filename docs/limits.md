# wdndds ComputeRPC size limits (post-v0.7)

This document records what payload sizes wdndds v0.7 can carry over the
wire, and where remaining size-related limits live (so the next person
debugging "why does my 5 MB payload get truncated?" doesn't have to dig
through three repos).

## TL;DR

| Layer | Limit | When it bites | Fix / workaround |
|-------|-------|--------------|-------------------|
| IDL field type (per-field) | **unbounded** (`sequence<octet>`) | never (post-v0.7) | n/a |
| `max_serialized_type_size` (per-message) | **1 MB** (hard) | single message > 1 MB | see below |
| UDP per-fragment | 65 500 B | never — FastDDS auto-fragments | n/a |
| FastCDR default `string` cap | 255 B | only `WdnddsStringTopicDataType` (capability registry, not ComputeRPC) | use `register_string_type` (already 4 MiB) |
| wdnwebui main-process RAM (per entity) | 16 samples × 1 MB = 16 MB hard | OOM if entity count grows past budget | `apply_bounded_resource_limits()` in `wdndds_cshim.cpp:220` |

## What v0.7 (方案B) actually changed

Before v0.7, eleven heavy-payload fields in `idl/compute.idl` were declared
as `string<65535>`:

```text
ComputeRequest:        prompt, model_id
ComputeReply:           error, status_text, text, level, tool_call,
                         bbox_json, crop_b64, model, model_dir, init_error
ComputeAttachment:      mime_type
```

FastCDR's `fixed_string<MAX>::assign` silently truncates to MAX on
overflow, so a 6 s × 16 kHz mono s16le PCM chunk (192 KB → base64 256 KB →
JSON envelope ≈ 256 KB) stuffed into `prompt` got truncated to 64 KB and
`json.loads` failed on the worker side → `LLMSession._dispatch` fell back
to `run_text()` →

    RuntimeError: OmniBackend.run_text(): pass AudioAttachment(wav_bytes)
    to LLMSession.respond() and the SDK will route to run_audio().

v0.7 makes all 11 fields `sequence<octet>` (i.e. `std::vector<uint8_t>`).
**Per-field there is no longer a cap.** Wire encoding (u32 LE length +
payload) is unchanged, so the bytes on the wire look identical; only the
C++ struct layout differs. (This is a **wire-protocol breaking change**:
old and new `.so` cannot talk to each other.)

Verified byte-exact end-to-end:

```text
PCM payload: 192000 bytes (sha=43c6d0f8435f879e)
client sending: prompt_len=256163
✅ 192000 bytes audio PCM roundtripped through new sequence<octet>
   prompt field, byte-exact.
```

E2E on the metro video: 36/36 chunks succeed, 0 errors.

## The remaining 1 MB cap and where it lives

`cshim_src/wdndds_cshim.cpp:427-428`:

```cpp
internal->req_type->max_serialized_type_size = 1048576;
internal->rep_type->max_serialized_type_size = 1048576;
```

This is a **per-message** cap, not a per-field cap. It bounds how large the
fully-serialized CDR message can be. The cap exists because we apply
`apply_bounded_resource_limits(qos, 8, 16)` to every writer/reader
(cshim.cpp:220) to bound per-entity RSS, and the resource_limits' pool
sizing depends on `max_serialized_type_size × allocated_samples`. Going
unbounded here would let the pool grow back to ~63 MB per entity
(see the long comment at cshim.cpp:200-218 explaining the v0.6.1
RSS-bleed fix).

### What's safe today under 1 MB

All current wdnwebui callers fit comfortably:

| Caller | Per-call message size | Headroom |
|--------|------------------------|----------|
| `audio_analysis.py:34` `CHUNK_SECONDS = 6.0` audio PCM | 192 KB raw → 256 KB base64 → ~256 KB envelope | 4× |
| `audio_analysis.py` 12 s audio | ~512 KB envelope | 2× |
| `detector.py` / VLM 1 frame (image, 1 MB JPEG via /dev/shm vd_*) | ~1 MB JPEG + ~100 B prompt | critical but works |
| `text_chat` `chat_run` history | ~tens of KB | plenty |
| `video_content_search` `embed_video` (4 s chunk, 512-d fp32) | ~2 KB | trivial |

### When the 1 MB cap would bite

| Scenario | Size | Would 1 MB cap? |
|----------|------|-----------------|
| Audio chunk > 6 s (e.g. configurable `CHUNK_SECONDS = 30`) | 1.6 MB raw → 2.1 MB base64 → ~2.1 MB envelope | **yes** |
| Single 1080p JPEG image via /dev/shm path (currently JPEG inline → base64) | 1 MB JPEG + ~100 B prompt | borderline |
| >2 MB base64 image inline (if JPEG path-list disabled) | 2.6 MB | **yes** |
| Long LLM output (single text reply > 1 MB) | >1 MB | **yes** |

Symptoms when the cap bites: FastCDR's `serialize()` returns `false`,
the writer logs a serialization error, and the client gets
`{"ok": false, "error": "..."}` from the reply side. Unlike the old
`string<65535>` bug, this error is **loud**, not silent.

### How to raise it (when needed)

Three coupled knobs in `wdndds_cshim.cpp`:

1. **`max_serialized_type_size`** — bump from 1 MB to the new max.
2. **`apply_bounded_resource_limits(writer_qos, allocated, max_total)`**
   and the reader variants — bump `max_total` so the resource_limits
   pool can hold `max_serialized_type_size × allocated_samples`.
3. **Resource limit on entity count** — if every entity now allocates
   much more per pool slot, the per-Participant RSS budget tightens.

The cost is linear in RSS:

```text
old (1 MB cap):   16 slots × 1 MB  =  16 MB per entity
raise to 4 MB:    16 slots × 4 MB  =  64 MB per entity
raise to 16 MB:   16 slots × 16 MB = 256 MB per entity
```

For wdnwebui's current scale (one worker per model × 7 models = 7
entities) the 16 MB cap is fine; raising to 4 MB is cheap; 16 MB would
start to bite.

Recommendation: **don't touch until you hit a real 1 MB-bite case.**
The current 6 s audio chunk + 1 MB image bounds leave plenty of
headroom. If/when the bound bites, raise to 4 MB first and re-verify
RSS under `worker_alive=True` for a few days.

## What about the `string<MAX>` / 255 B cap?

That's a separate code path: the C++ helper
`WdnddsStringTopicDataType` (sibling repo `wdndds_cxx/`) used by the
**capability registry** (registry topics in `wdndds/__init__.py`,
not ComputeRPC). It already has its own 4 MiB cap (see
`README.md` "Why a bundled C++ helper"). ComputeRPC's 11 fields no longer
hit this path after v0.7.

## Why not just remove `max_serialized_type_size`?

The regenerated `idl/computePubSubTypes.cxx` already overwrites
`max_serialized_type_size` per-message via `ComputeRequest_max_cdr_typesize`
/ `ComputeReply_max_cdr_typesize` (auto-calculated from the current
field widths). If we delete the explicit `1048576` assignment, the
resource_limits pool would size itself off the *real* payload (which
could be huge), re-introducing the v0.6.0 RSS bleed fixed in v0.6.1.
So the line is **load-bearing for the RSS bound**, not just a
truncation safety.

## Related documents

- `idl/compute.idl` — current IDL field declarations
- `cshim_src/wdndds_cshim.cpp:200-218` — long comment explaining the
  RSS-bleed fix that motivated the 1 MB cap
- `cshim_src/wdndds_cshim.cpp:427-428` — the cap itself
- `cshim_src/wdndds_cshim.cpp:219-227` — `apply_bounded_resource_limits`
- `v0.7 release commit` — string<65535> → sequence<octet> migration
