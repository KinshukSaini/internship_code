# Production Polish Plan — July 2026

Research synthesis: what the assembly pipeline needs to go from "clips stitched together" to a production-finished UGC ad (captions, transitions, color, audio mastering, hook/CTA). Combines a codebase audit with 2025–26 short-form best-practice research.

## Where the pipeline stands today

| Area | Current state |
|---|---|
| Assembly | concat-demuxer stream-copy of uniformly encoded clips (libx264 CRF16, medium, yuv420p, 24fps, 720p/1080p 9:16) — `app/stitcher.py`, `app/assembly.py` |
| Captions | Karaoke ASS (`\kf` word wipe) from ElevenLabs char alignment — fully built in `app/captions.py` but **off by default**, not exposed in UI/`generate_ugc_video()` |
| Transitions | None — hard cuts only |
| Color | `grade_phone_capture()` (CA + grain + vignette + eq) exists but gated behind `PHONE_CAPTURE_GRADE=False`, not in UI |
| Audio finishing | Nothing — no loudnorm, no music bed, no ducking, no fades, no room tone. AAC at default ~128k |
| Hook/CTA | Nothing — no text hook overlay, no end card |
| Colorspace | No BT.709 tagging on output |

## Priorities

### P0-A — Captions, done right (biggest single lift)

Research: word-pop captions are non-optional for paid UGC in 2025–26 (>80% sound-off viewing, +12–25% watch time). Dominant style = "Hormozi-preset": 1–3 word chunks, bold geometric sans, white base + accent highlight, thick black stroke + shadow, **lower-middle third** — NOT a letterbox band below the video (that's where TikTok/Reels chrome + native captions live; kreatli safe zones: bottom ~320–420px busy at 1080×1920).

Fixes to `app/captions.py` (infrastructure already correct, params wrong):
1. **`play_res_x/y` must derive from `clip_resolution`** — hardcoded 720×1280 breaks 1080p coordinate mapping.
2. **`words_per_line` 4 → 2–3** for 9:16 legibility.
3. **Font Arial → Montserrat Bold/Poppins (vendor the .ttf, pass `fontsdir=` to the subtitles filter)**; size ≈ 7–8% of frame height (≈95 @720p, ≈140 @1080p); Outline 8, Shadow 4 (scaled for 720p).
4. **MarginV into the safe lower-middle third**: ≈450/1920 (≈280/1280) — above platform chrome, below the face.
5. **Optional upgrade from `\kf` wipe → per-chunk pop**: one Dialogue event per 1–3-word chunk with `\t(0,120,\fscx110\fscy110)\t(120,200,\fscx100\fscy100)` scale-pop, `\an5`+`\pos`. This is the actual 2026 look; `\kf` fill is the 2023 look.
6. **`strip_alignment_tags`** before word grouping so `[EXCITED]` etc. never reaches burned text (known risk, docs/ai-ugc-research.md:180).
7. **Wire it up**: `burn_captions` param on `generate_ugc_video()`, UI toggle, default ON.

Emphasis keywords (product name, price, "FREE") can get the accent color via `\1c&H00FFFF&` injection — LLM already knows the script, so it can mark emphasis words at planning time.

### P0-B — Loudness mastering (cheap, mandatory)

No normalization exists; native-mode concats mix r2v speech + VO slices at unmatched levels. Fix: **two-pass `loudnorm` to I=-14 LUFS, TP=-1 dBTP, LRA=11** with `linear=true` (one-pass pumps) + `-ar 48000` + `-b:a 192k` on the final master. Run it as part of the final finishing pass, after all audio assembly.

### P1-A — Single "finishing pass" architecture (elegance win)

Today caption burn is its own re-encode; phone grade is another. Combine into **one final re-encode** on the assembled master:

```
-vf "curves=preset=medium_contrast,eq=saturation=1.06:contrast=1.04,
     noise=alls=8:allf=t+u,vignette=PI/6,
     subtitles=captions.ass:fontsdir=./fonts"
-af "<two-pass loudnorm values>"
-c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p
-colorspace bt709 -color_primaries bt709 -color_trc bt709
```

Rules from research:
- **Grade BEFORE grain** (current `grade_phone_capture` chain has eq after noise — reorder).
- Grain must be **temporal** (`allf=t+u`) — static grain reads as dirty lens. `alls=8` subtle; >20 noisy. Grain also dithers away banding from contrast moves on flat AI gradients.
- Captions LAST in the chain so text stays crisp (no grain/grade on the text).
- BT.709 tagging prevents platform color shifts.
- Optional film LUT at ~60% strength: `split[a][b];[b]lut3d=film.cube[b];[a][b]blend=all_opacity=0.6` — later; the eq/curves version gets 80% of the value.

### P1-B — Transitions: DON'T add crossfades

Research is unambiguous: hard cuts are correct for UGC — crossfade/xfade reads as "corporate slideshow" and is an AI tell. The pipeline's fast hard cuts are already right. The only worthwhile accent:
- **Punch-in** on product reveal / before-after beats: instant `scale=1.12*iw:-1,crop=iw/1.12:ih/1.12` on the accent beat's clip (or a 3–4 frame zoompan ramp). One accent style, used sparingly (1–2 per ad).
- Skip whip pans, wipes, speed ramps (ramps break lipsync/caption timing).

### P2 — Sound design + hook/CTA

1. **Music bed** under VO at low level, **sidechain-ducked** (`sidechaincompress=threshold=0.02:ratio=8:attack=20:release=300`, VO as key via `asplit`), doubles as room tone so inter-beat gaps never hit digital silence. Needs a small licensed/generated music library — new asset dependency, hence P2.
2. **Audio+video fade-out** on last 0.4s (`afade=t=out`, `fade=t=out`).
3. **Text hook** burned 0–2.5s (upper-middle, inside safe zone) — ASS event; hook text comes from the script LLM.
4. **CTA end card** from ~70% mark held to end: on-screen text + spoken CTA (dual-layer converts up to ~56% better than either alone). ASS event + script-planned CTA line.

### P3 — Later
- Cross-clip color matching (skimage `match_histograms` on reference frames → per-clip Hald CLUT → `lut3d`) for Seedance-vs-broll palette drift.
- Beat-synced cutting (librosa) once a music bed exists.
- Platform export variants.

## Tooling
- **pysubs2** for ASS authoring (only new required dep). Keep raw ffmpeg subprocess (existing pattern) — avoid MoviePy (slow, weak text).
- Vendor caption font .ttf in repo (`fonts/`), pass `fontsdir` — otherwise libass silently substitutes.
- ASS colors are `&HAABBGGRR` (BGR, inverted alpha) — the existing yellow `&H0000FFFF` is already correct.

## Encode-chain note
Final chain becomes: clips CRF16 → concat (copy) → VO overlay (copy) → **one** finishing pass CRF18. Two lossy generations total — fine. Do not let captions/grade/loudnorm each be their own pass.

## Sources
Caption styles/fonts/placement: blitzcutai.com, aividgenie.com, opus.pro. Safe zones: kreatli.com (TikTok/Reels), adaptlypost.com. ASS tags: aegisub.org. Grading AI video: invideo.io, reelmind.ai, jeffgeerling.com (lut3d), gabor.heja.hu (haldclut). Loudness: dev.to two-pass loudnorm, criticallisteninglab.com, clickyapps.com. Hook/CTA: sovran.ai, conbersa.ai, mbadv.agency. Codebase audit: this repo (assembly.py, captions.py, stitcher.py, config.py) as of 2026-07-09.
