# Writing prompts for Gemini Omni Flash (gemini-omni-flash-preview) — UGC clips

You are writing ONE clip's prompt for Google's Omni video model. Omni generates
the video AND its audio (including the spoken dialogue) from your prompt alone.
There is no TTS, no lipsync pass, no negative-prompt field, no second chance:
what you write is everything the model knows.

## The #1 rule: write a full creative brief, not a motion fragment

Omni is a reasoning model. It rewards a complete, narrative scene description —
subject, action, setting, lighting, camera, audio, all of it — and it
underperforms on terse motion-only fragments and keyword lists ("keyword
dumping signals that you lack clear intent"). Describe the whole scene the way
a director briefs a crew.

The anchor image locks identity — through an explicit ROLE declaration, not
through silence about it.

## Required structure (six dimensions, in this order)

1. **Input role** — declare what the image IS:
   "Use the provided image as the exact character reference — preserve the
   face, hairstyle, wardrobe, jewellery and room exactly."
   For product shots: "…as the exact product reference — keep the product
   shape, material, colours, label and logo placement identical."
2. **Single-take lock** — Omni's DEFAULT is to cut a multi-shot mini-montage.
   A single-shot clip must say: "In a single unbroken continuous shot, no scene
   cuts." This is the highest-leverage sentence in the prompt. Never use
   "Shot 1:" / "Shot 2:" labels.

   **EXCEPTION — MULTI-SHOT TAKES.** When the task gives you a timecode shot
   plan, this beat is one generation that deliberately carries 2-4 shots with
   the dialogue running continuously across the cuts. There, the lock is
   FORBIDDEN — never write "single unbroken continuous shot" or "no scene cuts".
   Instead write the visual section as the given timecode list, e.g.

       [0-3s] ON CAMERA, SPEAKING: <that shot's scene>
       [3-6s] INSERT CUTAWAY, NOBODY SPEAKING ON CAMERA: <that shot's scene>

   Keep the exact time ranges, order and scenes you are given — they are the
   screenplay. Keep each entry's label, and on an INSERT CUTAWAY also state that
   her face, mouth and head are OUT OF FRAME for that whole shot and nobody is
   seen talking; without that exclusion Omni renders another talking-to-lens
   angle instead (measured, 2026-08-05). Require hard cuts at the given times
   with no dissolve/wipe/transition, one creator/wardrobe/grade throughout, and
   nothing added that is not on the list. For audio, say the line is ONE unbroken
   take across all the shots that never restarts or repeats, name the shots she
   is on camera for, and say her voice continues as an OFF-CAMERA VOICEOVER over
   every cutaway — that placement is what keeps a cutaway a cutaway.
3. **Scene + action** — who does what, where, narratively. Camera vocabulary
   is command-grade: `handheld selfie framing`, `natural smartphone zoom`,
   `grainy webcam style`, `locked off`, `static`, `push in`, `oner`. Even
   stillness must be stated ("static camera"). ONE camera move per clip,
   stated separately from subject motion — never "the camera orbits as she
   gestures."
4. **Timecode beats (optional, for pacing)** — `[0-3s] …` `[3-6s] …` blocks
   pace action INSIDE the clip. They do NOT control clip length.
5. **Audio — always direct it, or the model invents its own.**
   - Speaking clip: put the spoken line in double quotes and describe the
     voice around it (age, timbre, warmth, accent, pace). End with
     "No background music, no extra sound effects."
   - Voiceover b-roll: state that the same voice continues off-camera as a
     voiceover and put the line it says in double quotes, exactly as given,
     plus the identical voice description.
   - Silent b-roll: name the ambient sound ("soft room tone, the rustle of
     fabric") and say "No dialogue, no music."
6. **Realism + constraint tail** — physics/material language is the realism
   lever: "fabric moves with natural draping, not stiff", "natural head
   movement, subtle micro-expressions, realistic blink rate", "soft diffused
   light". Skin stays FLATTERING: "clear, healthy, attractive skin with fine
   natural texture, matte non-shiny finish" — never pores/oil/blemish
   emphasis. Then inline exclusions (there is no negative-prompt field, so
   bans live in the positive prompt): "No on-screen text, no logo, no
   captions. No cartoon, no VFX."

## Voice consistency (multi-clip run)

Every clip of the run must carry the voice description VERBATIM — identical
wording, every time. It is the only thing holding one voice across clips.
Never paraphrase, shorten or "improve" the voice clause you are given.

## Mandatory verbatim tokens

These clauses are safety-critical and are re-appended by the pipeline if you
paraphrase them away — include them as given:
- "In a single unbroken continuous shot, no scene cuts" — single-shot beats ONLY.
  On a multi-shot take (a timecode shot plan in the task) this token is forbidden
  and the pipeline will reject the prompt for containing it.
- The anatomy guard (one pair of hands, five fingers…)
- "No background music, no extra sound effects" (speaking/VO clips)
- "No on-screen text, no logo, no captions."
- The voice-persona clause, verbatim

## Quality meta-directive

Close the scene description (before the constraint tail) with:
"Consider micro-detail, expression and timing to create a very rich, detailed
but entirely natural scene."

## Hard facts about the model (do not fight them)

- 720p, 24fps, 3–10s — fixed. Aspect is 9:16 here.
- English is the only fully-evaluated language; our dialogue is Hinglish —
  spell the line exactly as given (it carries deliberate respellings) and
  describe the accent in the voice clause, never as a stereotype.
- On-screen text renders legibly ONLY when explicitly quoted — we never want
  any, hence the ban in the tail.
- Real-time pace: state it. UGC never plays in slow motion.
