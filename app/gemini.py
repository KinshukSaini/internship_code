"""Google Gemini integration. Async-only.

Pure functions wrapping client.aio.models.generate_content. Each function
takes plain inputs and returns a structured Pydantic model.

GeminiClient now fronts BOTH text-LLM providers: every task method routes
through one _generate seam — "claude" (the default) goes to ClaudeBackend,
"gemini" keeps the original generate_content path.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app import costs
from app.config import (
    CHUNK_SECONDS,
    GEMINI_FAST_MODEL,
    GEMINI_PRO_MODEL,
    Settings,
)
from app.voice import VoiceVibe, catalog_for_prompt


# ---------------------------------------------------------------------------
# Product → Creator-archetype mapping.
#
# describe_character shows this dict to Gemini and asks it to pick the
# archetype that best fits the product, then shape the character around it.
# Tweak these to steer what "good Indian Instagram creator" means for each
# product type — strongest steering signal after the brief itself.
#
# Editing rules:
#   - Keep archetype keys short and orthogonal.
#   - Each value describes the SUBJECT, not the SET — Gemini composes the
#     scene itself.
#   - Lean concrete on wardrobe + jewelry + hair + body energy. Avoid value
#     judgements like "beautiful"; say what the LOOK is.
# ---------------------------------------------------------------------------
CREATOR_ARCHETYPES: dict[str, str] = {
    "wellness_fitness": (
        "Fit, glowy wellness creator — toned arms, healthy skin with a "
        "subtle workout-flush, layered minimal gold jewelry, hair in a "
        "messy bun or natural waves, wears fitted ribbed tanks / "
        "athleisure / oversized white tees, no heavy makeup. Aspirational "
        "morning-routine energy."
    ),
    "food_kitchen": (
        "Warm, comfortable-in-the-kitchen Indian creator — natural wavy "
        "or curly hair, fitted neutral tees in cream / beige / soft "
        "olive, delicate gold pendant or huggie hoops, fresh-faced "
        "minimal-makeup look, easy expressive face. The kind of creator "
        "who actually cooks the meals she posts."
    ),
    "beauty_skincare": (
        "Polished, makeup-aware beauty creator — clean glass-skin look, "
        "well-shaped natural brows, modern nude or berry lip, hair styled "
        "distinctly (sleek claw-clip twist, blunt bob, or soft curls — "
        "vary it), a satin cami / structured blouse / soft knit. "
        "Sophisticated but approachable, the kind of creator who reviews "
        "skincare."
    ),
    "fashion_lifestyle": (
        "Fashion-forward urban Indian creator — trend-aware outfit "
        "(oversized button-down, fitted crop, structured pants, slip "
        "dress, co-ord set — pick a real look), statement jewelry that "
        "is NOT the default gold hoops (chunky silver, beaded, septum / "
        "nose ring, or none), hair sleek, braided, or in a sharp cut. "
        "Confident posture. Mumbai / Delhi creator vibe."
    ),
    "tech_gadget": (
        "Smart-casual modern Indian creator — fitted neutral tee or "
        "button-down, minimal jewelry, modern wristwatch, clean hair, "
        "alert engaged expression. Reads as a tech reviewer or productive "
        "young professional, not a fashion creator."
    ),
    "default": (
        "Modern aspirational Indian Instagram creator — naturally "
        "photogenic, natural healthy skin, fresh-faced minimal makeup, "
        "confident relaxed energy. Give her/him a SPECIFIC look of your "
        "choosing (wardrobe colour + one distinctive detail — a printed "
        "shirt, a bandana, glasses pushed up, a streak of colour in the "
        "hair, one bold accessory) so no two casts read the same. "
        "50k–500k follower kind of creator, not a stock model and not an "
        "editorial cover face."
    ),
}


# ---------- Pydantic schemas ----------

class ScriptDraft(BaseModel):
    full_script: str = Field(description="Final dialogue/voiceover, ready to read aloud.")
    tone_notes: str = Field(description="One-sentence direction on tone, pace, energy.")
    n_chunks: int = Field(description="Number of 15-second video chunks needed.")


FramingChoice = Literal["selfie", "tripod", "counter", "walking"]


class SceneBeat(BaseModel):
    """One visual beat in the screenplay, aligned to a 15s chunk."""
    index: int
    visual_action: str = Field(
        description="Rich 1-2 sentence description of what's on screen "
                    "during this 15s window.",
    )
    framing: FramingChoice = Field(
        description="Camera framing for this chunk: selfie (handheld phone), "
                    "tripod (static medium shot), counter (looking down at "
                    "surface), or walking (handheld through space).",
    )
    camera_intent: str = Field(
        description="One camera move for this slice: static, slow push-in, "
                    "slow pull-out, tilt-down, tilt-up, pan-left, pan-right.",
    )


class Screenplay(BaseModel):
    """Deliberate visual screenplay produced before dialogue distribution."""
    overall_arc: str = Field(
        description="One-paragraph description of the visual flow across "
                    "all chunks — what changes scene-to-scene.",
    )
    scene_beats: list[SceneBeat] = Field(
        description="Exactly one beat per chunk, ordered by index.",
    )


# ── Phase-4 timeline planner (replaces screenplay + split_script) ───────────
# The video is ONE continuous VO track; the visual track cuts between
# talking-head shots (face on camera, lipsynced) and B-roll cutaways
# (product/hands/scene, silent, VO playing over). Every beat carries the
# slice of dialogue spoken during it, so concatenating all beats'
# dialogue_text reconstructs the full script — that lets us map each beat to
# a precise [start,end] time window using ElevenLabs character timestamps.
#
# Format library (Level-2): formats are explicit selectable enums so the
# choice is steerable, testable, and overridable per request.

ShotType = Literal["talking_head", "broll"]

VoiceMode = Literal["on_camera", "background_vo", "silent"]

# Talking-head camera formats. Descriptions live in the Field below so the
# planner LLM knows what each means when choosing.
TalkingFraming = Literal[
    "selfie",                 # handheld front-camera at arm's length, casual confessional
    "lav_mic",                # handheld wireless mic near the mouth — desi creator signature
    "tripod",                 # propped static medium shot, hands free
    "walking",                # walk-and-talk, handheld, movement energy
    "approach",               # walks toward camera, frame goes wide→tight, max energy (hooks)
    "confessional",           # very tight intimate close-up, talking-to-a-friend, emotional
    "mirror",                 # phone held to a mirror, reflection visible (outfit/result reveal)
    "grwm",                   # get-ready-with-me — doing makeup/hair while talking
    "punch_in",               # static then a rapid zoom-in on the face for emphasis on a key line
    "product_reveal_pivot",   # talking head then turns/reaches to grab the product into frame
    # ── UGC framing taxonomy (docs/framing-taxonomy.md, 2026-07-31) — real
    # creator shot vocabulary added for screenplay variety. Camera geometry per
    # frame lives in _FRAMING_CAMERA_HINT (anchor composition) and
    # pipeline._FRAMING_MOTION_CAMERA (motion behaviour).
    "front_pano",             # still camera above head level, slightly zoomed out, creator talks centred
    "floor_shot",             # still camera ON the floor, angled ~45° up at the creator
    "high_angle_drop",        # handheld above head on one side, drops to head level front-on (2-phase — sparing)
    "pan_right",              # still creator, camera pans left→right at eye level
    "pan_left",               # still creator, camera pans right→left at eye level
    "low_angle_table",        # still camera resting on a table, front or slightly angled
    "pov_walk",               # walking, camera at eye level angled down, body visible
    "follow_turn",            # camera follows from behind while walking; creator turns to the lens (2-phase — sparing)
    "top_high_angle",         # still camera well above head looking down, full body visible
    "side_track",             # camera moves WITH the walking creator, directly from the side
    "closeup_chest",          # still camera at chest level facing up at the creator, front or side
    "found_it",               # creator walks into a still chest-level frame, finds the lens
    "top_angle",              # still 90° overhead — creator lying/looking straight up
    "wide_side_low",          # still wide from the side at waist level, creator seated
    "vlog",                   # handheld at shoulder level facing the creator as they move around
]

# B-roll cutaway shot categories.
BrollType = Literal[
    "product_closeup",        # macro of the product itself
    "hands_using",            # hands applying / using / holding the product
    "unboxing",               # opening the packaging
    "before_after",           # split or sequential result comparison
    "texture_pour",           # macro texture / swatch / pour / sizzle
    "product_in_scene",       # styled product in an environment / flat-lay
    "demo_steps",             # step-by-step usage shots
    "lifestyle",              # environment / lifestyle context shot (no product)
]

# Hook style for the first 0-3 seconds (pattern interrupt).
HookType = Literal[
    "pov",                    # "POV: you've tried every X and still Y"
    "stop_scrolling",         # "Stop scrolling if you're a [audience]"
    "controversial",          # "This might be controversial, but..."
    "curiosity_gap",          # "Nobody tells you that..." (opens an info loop)
    "question",               # a provocative question
    "listicle",               # "3 reasons..." / "5 things..."
    "result_first",           # leads with the outcome ("This faded my dark spots in 3 weeks")
    "callout",                # "If you're a busy [audience]..."
    "myth_buster",            # "I thought this was a scam until..."
]

# Overall narrative arc.
NarrativeStructure = Literal[
    "pas",                    # Problem → Agitate → Solution → CTA (the workhorse)
    "storytime",              # first-person mini-story; product is the turning point
    "day_in_life",            # casual routine vlog with the product woven in
    "testimonial",            # genuine review / personal recommendation
    "comparison",             # this vs that (old solution vs the product)
    "listicle",               # numbered tips / reasons
    "demo",                   # how-to / tutorial focused
]

# The fixed UGC ad anatomy position of each beat — enforces structure.
AdSection = Literal[
    "hook",                   # 0-3s — the pattern interrupt
    "problem",                # 3-10s — name the pain / context
    "solution",               # 10-20s — the product / demo (pays off the hook)
    "proof",                  # before/after, testimonial, social proof
    "cta",                    # last 3-5s — ONE call to action
]


class TimelineBeat(BaseModel):
    index: int
    ad_section: AdSection = Field(
        description="Which part of the ad anatomy this beat belongs to. "
                    "Beats MUST be ordered hook → problem → solution → proof "
                    "→ cta. hook is first (0-3s), cta is last (3-5s). proof "
                    "may be folded into solution.",
    )
    dialogue_text: str = Field(
        description="VERBATIM contiguous slice of full_script spoken during "
                    "this beat. Preserve EVERY character — ellipses (…), "
                    "em-dashes (—), CAPS emphasis, and any [audio tags] — do "
                    "NOT normalize, re-punctuate, or trim. Concatenating every "
                    "beat's dialogue_text in index order MUST reconstruct "
                    "full_script EXACTLY (same characters/spaces/punctuation). "
                    "Never split an [audio tag] across two beats.",
    )
    shot_type: ShotType = Field(
        description="talking_head = the creator's face is on camera speaking "
                    "(will be lipsynced). broll = a cutaway (product/hands/"
                    "scene) while the voiceover continues underneath.",
    )
    framing: TalkingFraming | None = Field(
        default=None,
        description="talking_head ONLY (null for broll). Pick from the full "
                    "UGC frame vocabulary and VARY it — never the same framing "
                    "on two consecutive talking beats. Handheld/moving: "
                    "selfie, lav_mic, walking, approach, vlog, pov_walk, "
                    "side_track, high_angle_drop (2-phase, ≤1/video), "
                    "follow_turn (2-phase, ≤1/video). Still camera: tripod, "
                    "confessional, punch_in, front_pano, floor_shot, "
                    "low_angle_table, closeup_chest, found_it, top_angle, "
                    "top_high_angle, wide_side_low, pan_right, pan_left "
                    "(camera pans, creator still). Special: mirror, grwm, "
                    "product_reveal_pivot (turns to bring product in).",
    )
    broll_type: BrollType | None = Field(
        default=None,
        description="broll ONLY (null for talking_head). The cutaway "
                    "category: product_closeup, hands_using, unboxing, "
                    "before_after, texture_pour, product_in_scene, "
                    "demo_steps, lifestyle.",
    )
    broll_shot: str | None = Field(
        default=None,
        description="broll ONLY. Free-text specifics of the cutaway, e.g. "
                    "'macro of the serum dropper with a drop forming'. Null "
                    "for talking_head.",
    )
    visual_note: str = Field(
        description="One sentence describing exactly what's on screen during "
                    "this beat. Write it KNOWING the creator's look and setting "
                    "(from the character description) — a physically-plausible, "
                    "staged moment, not a stock direction. ONE motion verb + ONE "
                    "camera intent; never stack subject motion AND camera motion. "
                    "Product shots use a STATIC camera. The product is held by "
                    "exactly ONE pair of hands (no caressing / extra / phantom "
                    "hands). On a talking_head, the face is never obscured by a "
                    "hat brim or sunglasses over the eyes.",
    )
    voice_direction: str = Field(
        default="",
        description="How this specific LINE is DELIVERED vocally — you are "
                    "directing the read, so vary it beat to beat with the "
                    "emotional arc instead of repeating one register. Use "
                    "exactly this shape, under 14 words: 'In a (emotional "
                    "state) state, with a (tone) tone and a (pace) pace'. "
                    "Example: 'In a delighted, surprised state, with a warm, "
                    "brightening tone and a lively, real-time pace'. HARD RULE: "
                    "the pace must always be a BRISK/real-time variant (fast, "
                    "brisk, lively, steady) — never 'slow', 'unhurried' or "
                    "'drawn out', because the native video model STRETCHES "
                    "speech to fill the clip when told to slow down. Empty "
                    "string for reaction/silent beats (no speech to deliver).",
    )
    beat_kind: Literal["speech", "reaction"] = Field(
        default="speech",
        description="'speech' = the creator is speaking the dialogue_text this "
                    "shot. 'reaction' = a SHORT silent screen-time hold with NO "
                    "speech (a beat to let a line land). Keep the staged reaction "
                    "SUBTLE and natural — a slight head turn, a soft breath, a "
                    "small knowing smile, eyes softening. AVOID exaggerated or "
                    "literal poses (eyes squeezed shut, face tilted straight up, "
                    "theatrical sniffing) — they read awkward. A reaction beat "
                    "MUST have an empty dialogue_text and set hold_s.",
    )
    hold_s: float | None = Field(
        default=None,
        description="Reaction beats ONLY: seconds of silent screen time to hold "
                    "(e.g. 0.8). Null on speech beats.",
    )
    shows_creator: bool = Field(
        default=True,
        description="Is the creator's face or body on screen this shot? True for "
                    "talking_head and any broll that shows the person or her "
                    "hands; False for a pure product macro / a scene with no "
                    "person in it. Drives whether the character reference is "
                    "attached for this beat.",
    )
    product_in_frame: bool = Field(
        default=False,
        description="Is the product VISIBLE anywhere in this shot, in ANY way? "
                    "True if she is holding it, using it, pouring it, or if it "
                    "is simply sitting on a counter/table/shelf, in her hand, "
                    "or anywhere in the background — even far away, partly out "
                    "of frame, or out of focus. This is NOT limited to dedicated "
                    "product shots: a talking_head with the bottle behind her on "
                    "the counter is True. Only False when the product is "
                    "genuinely absent from the frame. Drives whether the real "
                    "product photo is attached as a reference for this beat, so "
                    "a False here on a shot that does show it makes the model "
                    "invent a fake product.",
    )
    voice_mode: VoiceMode | None = Field(
        default=None,
        description="How this beat's voice is delivered (native Seedance audio): "
                    "on_camera = creator speaks to lens (lip-synced); "
                    "background_vo = the line continues as off-screen voiceover "
                    "over a broll/product/action cutaway (THIS IS THE DEFAULT "
                    "for broll that has a line — the voice should keep flowing); "
                    "silent = NO dialogue this shot (just an action / holding "
                    "the product / a reaction hold) — set ONLY when the shot "
                    "genuinely carries no line. Most broll is background_vo; "
                    "silent is the exception.",
    )


class MotionPrompt(BaseModel):
    prompt: str = Field(
        description="The final Seedance video prompt for THIS single beat, "
                    "plain text. Camera and subject in separate sentences. "
                    "Resourceful and specific — every clause does work; no "
                    "filler, and never restate what the anchor image already "
                    "shows. No length cap.",
    )
    restaged: bool = Field(
        description="True ONLY if you changed the planner's staged action / "
                    "scene for this beat; False if you kept the planner's "
                    "visual_note intent.",
    )


class Pronunciation(BaseModel):
    """One word the TTS is likely to mispronounce, plus how to say it.

    Emitted per RUN by the planner rather than kept in a static table: the
    planner knows which words in the script it just wrote are unusual (brand and
    product names, Sanskrit/Hindi terms, coined words) and how they sound. A
    hardcoded list would neither scale to new briefs nor be trustworthy.

    The SAME respelling feeds every engine, which is why it is a plain English
    respelling rather than IPA or ARPAbet:
      * ElevenLabs — an `alias` pronunciation-dictionary rule (works on every
        model; inline <phoneme> SSML does NOT work on eleven_v3),
      * Cartesia / Fish — substituted into the provider text,
      * Seedance native — substituted into the quoted line in the video prompt
        (BytePlus's documented fix is exactly this homophone respelling).
    """
    word: str = Field(
        description="The word EXACTLY as it appears in full_script "
                    "(e.g. 'Ayurvedic'). One word per entry — never a phrase.",
    )
    respelling: str = Field(
        description="Plain-English phonetic respelling, hyphenated by syllable "
                    "with the STRESSED syllable in CAPS — e.g. 'Ayurvedic' → "
                    "'aa-yur-VAY-dik'. Use only ordinary English letters and "
                    "hyphens: NO IPA symbols, no digits, no phoneme codes. "
                    "Spell it so an English speaker reading it aloud produces "
                    "the correct native pronunciation. Avoid letter pairs that "
                    "English reads as a different sound than intended (write "
                    "'p-huh' not 'pha' if you mean an aspirated p, since "
                    "English reads 'ph' as 'f').",
    )


class VideoTimeline(BaseModel):
    pronunciations: list[Pronunciation] = Field(
        default_factory=list,
        description="Words in full_script that a TTS trained mainly on Western "
                    "English would MISPRONOUNCE, with respellings. Include "
                    "Sanskrit/Ayurvedic and Hindi terms, brand and product "
                    "names, and any coined or unusual word. Omit ordinary "
                    "English words — every entry changes how the word is spoken, "
                    "so a wrong entry makes it WORSE. Empty list is correct when "
                    "the script has no such words.",
    )
    full_script: str = Field(
        description="The complete voiceover script at NATURAL speaking pace "
                    "for the target duration. This is what gets voiced. "
                    "Carries the prosody score: … pauses, — hard cuts, ONE "
                    "CAPS word per sentence for emphasis, and inline eleven_v3 "
                    "[audio tags] — roughly one DELIVERY tag per sentence (they "
                    "are what make the read expressive rather than monotone), "
                    "plus at most 2 audible REACTION tags in the whole script. "
                    "No SSML / <break> tags.",
    )
    target_duration_s: int = Field(
        description="Intended runtime in seconds. When the caller forces a "
                    "duration, use it; otherwise choose it yourself per the "
                    "instructions (15-45s for a fresh script; your estimate of "
                    "the spoken length for a verbatim script).",
    )
    script_source: Literal["verbatim", "generated"] = Field(
        default="generated",
        description="'verbatim' if full_script was copied WORD-FOR-WORD from an "
                    "explicit script already present in the brief; 'generated' "
                    "if you wrote it fresh from a rough idea. Set 'verbatim' "
                    "ONLY when the brief contained actual spoken dialogue you "
                    "preserved exactly.",
    )
    hook_type: HookType = Field(
        description="The opener style used in the first 0-3s.",
    )
    narrative_structure: NarrativeStructure = Field(
        description="The overall arc of the ad.",
    )
    beats: list[TimelineBeat] = Field(
        # An EMPTY list satisfies list[TimelineBeat], so a sample that returned
        # "beats": [] parsed cleanly and only failed 50 lines later in
        # _validate_timeline ("timeline has no beats"). Observed on the FIRST
        # attempt of both planning calls of 2026-08-06, each after ~19.5k output
        # tokens of adaptive thinking — the retry recovered, but the sample cost
        # real money and the schema never said a beat was required. min_length
        # puts minItems in the JSON schema the model is handed, so it is told up
        # front, and the SDK's own validation rejects it at the boundary.
        min_length=1,
        description="Ordered beats covering the whole script, following the "
                    "hook→problem→solution→proof→cta anatomy. Default to FAST "
                    "2-3s cuts (one beat per visual change, ≤8 spoken words); "
                    "AT MOST ONE beat in the whole video may run ~4-5s (≤11 "
                    "words) when the moment truly earns it (a key line, a "
                    "product hero). Beyond ~5s it gets auto-split.",
    )
    product_name: str | None = Field(
        default=None,
        description="The product's short display name as spoken in the ad "
                    "(e.g. 'GlowSerum'). Null for a no-product/lifestyle "
                    "brief. Used to highlight the name in burned captions.",
    )


class BriefDirectives(BaseModel):
    """Structured extraction of the brief's EXPLICIT instructions (2026-07-31).

    Runs BEFORE planning so that (a) a script the client wrote is detected
    once, up front, and then ENFORCED verbatim in code (the planner's own
    mode self-classification silently paraphrased user scripts), and (b)
    visual/screenplay directives survive past the planner to the per-beat
    motion-prompt writers (the planner prompt is ~250 lines — a directive
    buried in free text was routinely lost downstream)."""

    explicit_script: str | None = Field(
        default=None,
        description="The exact spoken script IF the brief contains one "
                    "(actual creator dialogue the client wrote — a labeled "
                    "'Script:' block, or lines of talking-to-camera copy). "
                    "COPY IT CHARACTER-FOR-CHARACTER: every word, capital, "
                    "punctuation mark, line break and parenthetical stage "
                    "direction exactly as the brief has it. null when the "
                    "brief is only an idea/product description. NEVER invent "
                    "or complete a script that isn't there.",
    )
    visual_directives: list[str] = Field(
        default_factory=list,
        description="Every EXPLICIT instruction about how the video should "
                    "LOOK or be shot: required shots/moments, framing/camera "
                    "rules, screenplay/structure asks, setting, props, "
                    "wardrobe, vibe/tone words, brand-visibility rules. One "
                    "directive per item, quoting the brief's wording as "
                    "closely as possible. Empty when the brief states none.",
    )
    character_directives: list[str] = Field(
        default_factory=list,
        description="Every EXPLICIT instruction about the creator/model "
                    "(gender, age, look, skin tone, hair, language/accent, "
                    "energy). Empty when the brief states none.",
    )


_SECTION_ORDER = {"hook": 0, "problem": 1, "solution": 2, "proof": 3, "cta": 4}
# Rank used for the non-decreasing ORDER check: solution and proof share a body
# rank because real UGC ads interleave feature/solution beats with proof beats
# (feature → proof → feature → proof). hook-first, problem-before-body, and
# cta-last-contiguous are still enforced; only solution↔proof may interleave.
_SECTION_RANK = {"hook": 0, "problem": 1, "solution": 2, "proof": 2, "cta": 3}


# Per-framing camera/composition hint injected into the first-frame image
# prompt. Most UGC framings are FRONT camera; only the mirror selfie uses the
# rear camera (phone visible in the reflection). The old prompt hard-coded a
# legacy 4-value list and mislabelled everything-not-selfie as "back camera" —
# wrong for 8 of the 10 framings the planner actually emits.
# These describe the resulting LOOK / camera POV — NOT a rig to render. The
# phone IS the unseen camera; naming "phone"/"tripod"/"propped" made the image
# model put the recording rig in frame (a tripod + phone beside the creator),
# illogical for UGC footage. The phrasing below avoids that, and the explicit
# no-rig rule in write_first_frame_prompt enforces it. mirror is the ONE
# intentional exception (the phone shows in the reflection).
_FRAMING_CAMERA_HINT: dict[str, str] = {
    "selfie": "arm's-length front-facing selfie framing, intimate and close",
    "lav_mic": "front-facing framing with a small wireless lav mic held near the mouth (the mic is the only visible gear)",
    "tripod": "locked, static eye-level medium shot",
    "walking": "walk-and-talk front-facing framing with gentle handheld sway",
    "approach": "front-facing framing, the creator walks toward the lens, wide→tight",
    "confessional": "tight, intimate front-facing close-up",
    "mirror": "rear-camera phone-to-mirror selfie — the phone IS visible in the mirror reflection (intentional for this framing only)",
    "grwm": "front-facing framing, getting ready (hair/skin) while talking",
    "punch_in": "static front-facing medium shot with a rapid zoom-in on the face",
    "product_reveal_pivot": "front-facing framing, the creator turns/reaches to bring the product into frame",
    # ── framing taxonomy (docs/framing-taxonomy.md) — the ANCHOR composition
    # for each frame; the camera MOVE (if any) lives in
    # pipeline._FRAMING_MOTION_CAMERA and only affects the motion prompt.
    "front_pano": "camera slightly above head level and a step back, creator centred and talking, a little zoomed out",
    "floor_shot": "camera resting on the floor angled ~45° up at the creator, low dramatic UGC angle",
    "high_angle_drop": "camera held above head height off to one side, angled down at the creator",
    "pan_right": "eye-level framing with the creator just left of centre, room visible to the right",
    "pan_left": "eye-level framing with the creator just right of centre, room visible to the left",
    "low_angle_table": "camera resting on a table surface, slight up-angle at the seated/standing creator, front or slightly angled",
    "pov_walk": "eye-level camera angled slightly down while the creator walks, upper body and outfit visible",
    "follow_turn": "camera behind the walking creator at head level, her head turning back toward the lens",
    "top_high_angle": "camera well above head level looking down, the creator's full body visible in the scene",
    "side_track": "camera directly beside the creator at shoulder level in profile as she walks",
    "closeup_chest": "camera at chest level tilted up at the creator's face, front or slight side angle",
    "found_it": "chest-level framing, the creator mid-step from one side, eyes just finding the lens",
    "top_angle": "directly overhead 90° top-down framing, the creator looking straight up at the lens",
    "wide_side_low": "wide shot from the side at waist level, the creator seated in the scene",
    "vlog": "shoulder-level framing facing the creator, casual handheld vlog energy",
}

# Per-framing CAMERA MOTION line (docs/framing-taxonomy.md) — how the camera
# BEHAVES during the clip, consumed by pipeline's deterministic talking-head
# builders AND surfaced verbatim to the Opus motion writer. The old builders
# hard-coded one handheld-selfie camera for every framing, so every shot moved
# identically regardless of the planner's pick — a top cause of "every video
# looks the same". Frames absent here (selfie, lav_mic, confessional, mirror,
# grwm, product_reveal_pivot) keep the handheld selfie default. ONE decisive
# move per frame (stacked moves are the #1 distortion source).
_FRAMING_MOTION_CAMERA: dict[str, str] = {
    "tripod": "The camera is still, propped at eye level",
    "front_pano": "The camera is still, slightly above head level and a "
                  "little zoomed out, while the creator moves and talks",
    "floor_shot": "The camera stays still on the floor, angled up at the "
                  "creator",
    "high_angle_drop": "The camera starts handheld above head level to one "
                       "side, then drops smoothly to head level directly in "
                       "front of the creator",
    "pan_right": "The camera makes one smooth eye-level pan from left to "
                 "right across the creator",
    "pan_left": "The camera makes one smooth eye-level pan from right to "
                "left across the creator",
    "low_angle_table": "The camera stays still on the table, angled slightly "
                       "up at the creator",
    "pov_walk": "The camera holds at eye level angled slightly down while "
                "the creator walks, with natural walking sway",
    "follow_turn": "The camera follows the creator from behind at head level "
                   "as she walks; she turns her head and finds the lens",
    "top_high_angle": "The camera is still, well above head level, looking "
                      "down at the creator",
    "side_track": "The camera moves with the walking creator, holding "
                  "directly at her side at shoulder level",
    "closeup_chest": "The camera is still at chest level, facing up at the "
                     "creator",
    "found_it": "The camera is still at chest level; the creator walks into "
                "frame, finds the lens and locks eyes",
    "top_angle": "The camera is still, directly overhead, looking straight "
                 "down",
    "wide_side_low": "The camera is still at waist level to the side, wide "
                     "on the seated creator",
    "vlog": "Handheld at shoulder level facing the creator, moving with her "
            "with a loose vlog sway",
    "walking": "Handheld walk-and-talk with a natural stride sway",
    "approach": "The camera holds while the creator strides toward the lens, "
                "the frame going wide to tight",
    "punch_in": "The camera makes one rapid, confident punch-in on the face "
                "for the key line",
}


_MOUTH_ACTION_RE = re.compile(
    r"\b(eat|eating|ate|bite|biting|chew|chewing|sip|sipping|drink|drinking|"
    r"swallow\w*|taste|tasting|lick|licking|brush\w*\s+teeth|"
    r"appl\w*\s+(?:to\s+)?(?:lip|lips|mouth|face)|spray\w*\s+\w*\s*mouth|"
    r"spoonful|mouthful|takes?\s+a\s+bite)\b", re.I)

# A talking_head is lip-synced AND face-on-camera. An accessory over the eyes
# (sunglasses, a hat brim pulled low) hides the face the lipsync needs;
# physically-illogical hand staging ("hands caress", "multiple/extra hands",
# "two pairs of hands") produces the phantom-hand artifact r2v can't recover
# from. Both are rejected at plan time where a retry is free.
# Matches eyewear/headwear that COVERS the face — NOT eyewear pushed up onto
# the head (the safe default), which must pass.
_FACE_OBSTRUCT_RE = re.compile(
    r"\b((?:sunglasses|shades|aviators?)\s+(?:on|over|covering|down\s+over|"
    r"hiding|obscuring)\s+\w*\s*(?:eye|eyes|face)|"
    r"(?:hat|cap|brim|hood)\s+(?:\w+\s+){0,2}(?:over|covering)\s+\w*\s*(?:eye|eyes|face)|"
    r"(?:obscur\w*|cover\w*|hidden|hiding)\s+\w*\s*face|"
    r"face\s+(?:obscured|covered|hidden))\b",
    re.I)
_ILLOGICAL_HANDS_RE = re.compile(
    r"\b(caress\w*|"
    r"(?:multiple|extra|several|many|two\s+pairs?\s+of|three|four)\s+hands|"
    r"hands?\s+(?:caress|stroking|stroke)|"
    r"disembodied\s+hands?|phantom\s+hands?)\b", re.I)

# Multi-step fine-motor hand actions — tying laces, knotting, threading,
# braiding — are PHYSICALLY beyond current i2v models when animated in full:
# frame-to-frame finger/object coordination fails and the fingers/laces bend
# and fuse (user-reported 2026-07-31: shoelace-tying came out with warped
# fingers and bent laces). The moment itself is NOT banned (user correction
# 2026-08-01: "you are straight up barring things") — it is RESTAGED: the
# anchor still catches the hands already mid-action (image models handle a
# static mid-pose fine), the motion prompt gets pipeline._FINE_MOTOR_CLAUSE
# force-appended (one small decisive movement, cut-on-action, no intricate
# multi-step finger work animated), and the planner prompt teaches the same
# cut-on-action staging. This regex is the detector both layers share.
_DEXTERITY_RE = re.compile(
    r"\b(tying|knotting|lacing|unlacing|threading|braiding|weaving|"
    r"crocheting|knitting|folds?\s+origami|"
    r"tie[sd]?\s+(?:\w+\s+){0,2}(?:lace|laces|shoelace|shoelaces|knot|bow|"
    r"ribbon))\b", re.I)

# eleven_v3 audio tags ([excited], [sighs], …) — spoken-instruction tokens,
# not spoken words. Stripped before any word counting.
_AUDIO_TAG_RE = re.compile(r"\[[^\]]*\]")

# Parenthetical stage directions in a client script — "(wink)", "(takes a
# bite)". Not spoken; the planner moves them into visual_notes, so the
# verbatim-script comparison must ignore them on both sides.
_PAREN_DIRECTION_RE = re.compile(r"\([^)]*\)")

# Production labels in client scripts — "HOOK VO :", "SCENE 2:", "CTA -" —
# copy-editing markup, NOT spoken words. Dev-reported 2026-08-01: the
# extractor copied "HOOK VO : \"People always ask..\"" verbatim, the planner
# (correctly) emitted only the spoken words, and the raw comparison rejected
# a correct plan at char 0 — then the "corrected" retry copied the labels
# INTO full_script and broke the beat partition instead. Labels are stripped
# both from the forced script we hand the planner AND inside _script_norm.
# Keyword-anchored (not any ALL-CAPS word + colon) so a spoken line like
# "OKAY: here's the thing" is never eaten.
_PRODUCTION_LABEL_RE = re.compile(
    r"^[ \t]*(?:(?:HOOK|BODY|CTA|VO|V\.O\.|VOICE[ -]?OVER|NARRATOR|SCENE|"
    r"SHOT|CUT|VISUAL|AUDIO|SCRIPT|LINE|BEAT|INTRO|OUTRO|PROBLEM|SOLUTION|"
    r"PROOF|SUPER|TITLE|ON[- ]?SCREEN(?:[ ]+TEXT)?)"
    r"(?:[ ]+(?:VO|V\.O\.|\d+[A-Za-z]?))?)[ \t]*[:\-–—][ \t]*",
    re.I | re.M)

# Wrapping quotation marks around scripted lines ('HOOK VO: "line"') — the
# quotes are markup, never spoken. Apostrophes are NOT touched (don't, it's).
_QUOTE_CHARS_RE = re.compile(r'["“”«»]')


def _strip_production_markup(s: str) -> str:
    """Reduce a client script to its SPOKEN text: production labels and
    wrapping quotes removed, the spoken words themselves untouched."""
    s = _PRODUCTION_LABEL_RE.sub("", s or "")
    return _QUOTE_CHARS_RE.sub("", s)


def _script_norm(s: str) -> str:
    """Normal form for the verbatim-script equality check: production labels,
    wrapping quotes, audio tags and parenthetical stage directions removed,
    whitespace collapsed. Words, case and spoken punctuation are NOT touched —
    those are exactly what 'verbatim' promises to preserve."""
    s = _strip_production_markup(s)
    s = _AUDIO_TAG_RE.sub("", s)
    s = _PAREN_DIRECTION_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()

# Hard per-beat word cap — the enforced fast-cut ceiling (2026-07-31: user bar
# is 2-3s per shot, 5s only when genuinely earned; the old 16-word cap allowed
# 7-10s shots that read as stuck/slow). At measured eleven_v3 pacing (1.6-2.2
# w/s, boosted 1.15x), ~8 words ≈ 2.5-3.5s. Over-cap beats with an interior
# phrase boundary are rejected at plan time (a retry is free); ONE beat per
# video may run to _MAX_HELD_BEAT_WORDS (the rare earned hold, ≈4-5s).
_MAX_BEAT_WORDS = 8
# The one earned hold: a single beat per video may carry up to ~11 words
# (≈4-5s) — a key line that needs to land, a product hero. A SECOND over-cap
# beat is rejected like any other.
_MAX_HELD_BEAT_WORDS = 11
# Absolute ceiling for a single UNSPLITTABLE sentence (one phrase-unit with no
# interior cut point). The soft cap above is a pacing target and must YIELD when
# a sentence genuinely can't be cut at a phrase boundary — otherwise
# the word cap and the no-mid-sentence-split rule are mutually unsatisfiable for
# fixed verbatim copy (assembly.split_long_windows still chops the visual). A
# GENERATED sentence past this ceiling is a runaway the model can reword;
# VERBATIM copy is exempt entirely (the creative team's call, like the total
# budget). ~14 words ≈ ~5-6s at eleven_v3 pacing.
_HARD_MAX_BEAT_WORDS = 14

# Hard per-beat word floor for talking_head. Seedance reference audio has a
# 2s minimum per lip-synced clip; at measured 2.0 w/s, 5 words ≈ 2.5s clears
# it with margin. A shorter line must merge into a neighbouring beat or ride
# a broll beat as VO.
_MIN_TALKING_HEAD_WORDS = 5

# An audio tag at the END of a beat's dialogue renders audio past the beat's
# last aligned word — it leaks into the NEXT beat's audio slice. Tags may
# only open a line.
_TRAILING_TAG_RE = re.compile(r"\[[^\]]*\]\s*$")

# Safe swap pool for the framing-variety repair: front-camera frames with no
# hand-slot cost and no special downstream wiring (no product logic, no
# rear-camera mirror exception), so a repeat can be swapped to any of them
# without changing what the shot SHOWS. Ordered by UGC frequency.
_VARIETY_SAFE_FRAMINGS = (
    "selfie", "closeup_chest", "front_pano", "punch_in", "low_angle_table",
    "confessional",
)

# Product-ish nouns in a typed-less broll beat's text — used ONLY by the
# broll_type=None repair to decide product_in_scene vs lifestyle. Generic
# container nouns + the word product itself; brand names can't be enumerated,
# but a planner writing a product shot almost always names the container.
_PRODUCT_WORD_RE = re.compile(
    r"\b(product|bottle|jar|tube|pack|packet|pouch|sachet|box|carton|tub|"
    r"serum|cream|lotion|shampoo|perfume|cologne|spray|dropper|label)\b",
    re.I)

# Slow-pace vocabulary in a beat's voice_direction. The schema description
# already demands a brisk/real-time pace (the video model STRETCHES speech to
# fill the ordered clip, so "slow" compounds into a sluggish read), but a
# schema description is advisory — this is the enforcement. Computable, so it
# is REPAIRED in code (substituted with "brisk"), never re-rolled.
_SLOW_PACE_RE = re.compile(
    r"\b(?:very\s+|extremely\s+)?(?:slow(?:ly|er|est)?|unhurried(?:ly)?|"
    r"leisurely|languid(?:ly)?|drawn[-\s]?out|dragging|sluggish(?:ly)?|"
    r"lingering|meditative|slow[-\s]paced)\b", re.I)

# ── eleven_v3 audio-tag allowlist ────────────────────────────────────────────
# Audio tags ARE the modulation mechanism on eleven_v3 — the sliders are not
# (stability only sets how RESPONSIVE the model is to them). An under-tagged
# script therefore reads monotone no matter how the settings are tuned. This
# used to cap the whole script at 1-2 tags, which measured as flat; ElevenLabs'
# own canonical example puts TWO tags in a single 15-word line.
#
# The split below is not cosmetic — it follows from the timing architecture:
#
#   DELIVERY tags only change HOW the following words are said. They add no
#   audio of their own, so they cost the timeline nothing and can be used at
#   every genuine delivery shift.
#
#   REACTION tags emit an actual SOUND that has NO characters in the alignment
#   (assembly.beat_windows). That audio lands in no beat window and opens a
#   hole; a measured 0.92s [sighs] desynced 6 of 9 beats before the window-snap
#   fix. They still work, but each one leans on that repair — so they stay rare.
#
# Constrain the VOCABULARY, not the count: the one real failure mode is a voice
# whose range doesn't match a tag, which makes it SPEAK the tag aloud.
_DELIVERY_TAGS = (
    "[excited]", "[happily]", "[curious]", "[sarcastic]", "[worried]",
    "[frustrated]", "[playfully]", "[cheerfully]", "[whispers]", "[softly]",
    "[rushed]", "[deliberate]", "[emphasized]", "[short pause]",
)
_REACTION_TAGS = ("[laughs]", "[giggles]", "[sighs]", "[gasps]", "[exhales]")

# Audible reaction tags allowed in the WHOLE script (each opens an alignment
# hole — see above). Delivery tags are governed by per-sentence guidance instead.
_MAX_REACTION_TAGS = 2

# Terminal characters a beat may end on so a cut lands at a phrase boundary
# (never mid-sentence). The em-dash "—" is a hard self-correction cut, also a
# valid edge. A newline is handled separately (verbatim lines need not be
# punctuated).
#
# The comma is deliberately NOT here. Adding it (2026-08-06) to make the fast-cut
# word cap bite on long comma-spliced verbatim sentences cascaded twice: as an
# interior-only cut point it made the cap and the beat-boundary rule mutually
# unsatisfiable (the planner burned all 3 retries on a 17-word sentence it was
# told to split at boundaries it was forbidden to end on), and once added here
# too, _snap_boundaries_to_phrase_edges snapped to the nearest comma and minted
# 1-word beats under the talking-head floor. The shot ceiling is enforced
# downstream instead — pipeline._omni_windows divides any beat past
# OMNI_SHOT_MAX_S — which needs no agreement between three separate rules.
_PHRASE_END_CHARS = frozenset({".", "?", "!", "…", "—", ":", ";"})


def _ends_at_phrase_boundary(spoken: str) -> bool:
    """True when ``spoken`` (audio tags stripped, trailing spaces removed) ends
    at a natural cut edge: terminal punctuation (. ? ! …), a clause-terminal
    colon/semicolon (a real spoken pause — "take note:" introducing a list),
    an em-dash, OR a dash written
    as an ASCII hyphen. A spaced hyphen ("... two slots -") or a double hyphen
    ("--") is a dash used as PUNCTUATION and is a legal edge; an unspaced hyphen
    ("well-") is a compound-word break and is NOT — the leading space is the
    discriminator. Verbatim scripts routinely use '-' where we emit '—'."""
    if not spoken:
        return False
    if spoken[-1] in _PHRASE_END_CHARS:
        return True
    if spoken.endswith("--"):
        return True
    return spoken.endswith("-") and len(spoken) >= 2 and spoken[-2].isspace()


def _repartition_onto_full_script(full_script: str, speech_beats: list) -> bool:
    """Re-slice ``full_script`` onto ``speech_beats`` when they already agree up
    to whitespace, so the beats concatenate to full_script EXACTLY.

    The planner routinely drops the spaces/newlines that sit BETWEEN beats when
    it slices full_script into contiguous pieces (an 11-beat plan → ~10 missing
    boundary spaces), which fails the exact-partition check even though no words
    differ. Rather than re-roll the whole plan on a spacing slip, walk both
    strings skipping whitespace: if every non-space character matches in order,
    reassign each beat's dialogue_text to its exact full_script span (whitespace
    and all). full_script (the VO source) is left untouched, so the VO/window
    alignment is preserved perfectly and no spoken content changes.

    Returns True when repaired (beats mutated in place), False when the content
    genuinely differs (a real word/tag mismatch) — the caller then raises so
    _with_retry surfaces an actionable diff to the planner.
    """
    n = len(full_script)
    spans: list[str] = []
    j = 0        # cursor into full_script (next unconsumed char)
    start = 0    # start of the current beat's span in full_script
    for bi, b in enumerate(speech_beats):
        for ch in (b.dialogue_text or ""):
            if ch.isspace():
                continue                       # tolerate the beat's own spacing
            while j < n and full_script[j].isspace():
                j += 1                         # skip full_script whitespace
            if j >= n or full_script[j] != ch:
                return False                   # genuine content divergence
            j += 1
        end = j
        # Trailing whitespace (incl. the final tail) joins the beat it follows;
        # trailing *content* is a genuine gap and is left for the caller to
        # reject (start != n below).
        while end < n and full_script[end].isspace():
            end += 1
        spans.append(full_script[start:end])
        start = end
        j = end
    if start != n:
        return False                           # full_script has leftover content
    for b, span in zip(speech_beats, spans):
        b.dialogue_text = span
    return True


def _has_interior_phrase_boundary(spoken: str) -> bool:
    """True when a phrase boundary occurs strictly INSIDE ``spoken`` (before its
    final word) — i.e. an over-length beat could be legally cut into smaller
    pieces. When False the beat is a single atomic sentence with no cut point,
    so forcing a ≤cap split is impossible and the word cap must yield.

    Shares _PHRASE_END_CHARS with _ends_at_phrase_boundary deliberately — a
    place this says you may CUT must be a place that one says a beat may END, or
    the two rules cannot both be satisfied. Before the comma was added there,
    this predicate answered "atomic" for almost every long sentence, which handed
    every over-cap beat the unsplittable+verbatim exemption and silently disabled
    the fast-cut word cap on BOTH generator paths — how a 20-word beat became one
    10s shot (2026-08-05).
    """
    body = spoken.rstrip()
    # Peel the trailing END boundary so it is not mistaken for an interior cut:
    # a trailing dash ("-- " / " -"), then any run of terminal punctuation
    # (handles "..." / "?!" where dropping one char would leave a stray ".").
    if body.endswith("--"):
        body = body[:-2]
    elif body.endswith("-") and len(body) >= 2 and body[-2].isspace():
        body = body[:-1]
    body = body.rstrip("".join(_PHRASE_END_CHARS) + " \n")
    if not body:
        return False
    if "\n" in body:
        return True
    if any(ch in _PHRASE_END_CHARS for ch in body):
        return True
    # a spaced ("two slots -") or double ("--") hyphen used as a dash
    return " -" in body or "--" in body


def _snap_boundaries_to_phrase_edges(tl: "VideoTimeline") -> None:
    """Deterministically repair beat boundaries that land mid-sentence.

    Legal cut positions in full_script are COMPUTABLE (after terminal
    punctuation / a dash / at a newline), so a mid-sentence boundary doesn't
    need a model re-roll — a live run exhausted every retry re-making the
    IDENTICAL mid-sentence split ('…you can use them for workout or'). Each
    illegal boundary snaps to the nearest legal cut position; when two beats
    straddle one phrase-unit with NO interior cut point, they MERGE into one
    beat that keeps the shot design of the member carrying more spoken words
    (its ad_section, except a group that ends the script keeps the LAST
    member's — preserving hook-first and cta-last). Only the dialogue
    partition moves; full_script — the VO source — is untouched, so VO/window
    alignment is preserved. Requires the exact-partition check to have passed;
    bails silently when it hasn't (the caller raises there first).
    """
    script = tl.full_script
    n = len(script)
    beats = tl.beats
    speech_idx = [k for k, b in enumerate(beats) if b.beat_kind != "reaction"]
    if len(speech_idx) < 2:
        return

    def _legal(j: int) -> bool:
        prefix = script[:j]
        if prefix.endswith("\n") or script[j] == "\n":
            return True
        if _TRAILING_TAG_RE.search(prefix):
            return False   # a cut right after an [audio tag] leaks its audio
        spoken = _AUDIO_TAG_RE.sub("", prefix).rstrip(" ")
        return _ends_at_phrase_boundary(spoken)

    ends: list[int] = []
    off = 0
    for k in speech_idx:
        off += len(beats[k].dialogue_text or "")
        ends.append(off)
    if off != n:
        return   # partition doesn't hold — not this repair's job

    groups: list[list[int]] = [[speech_idx[0]]]  # beat positions per span
    cuts: list[int] = []                         # chosen offsets, increasing
    last = 0
    changed = False
    for i in range(len(speech_idx) - 1):
        want = ends[i]            # the planner's boundary for this cut
        upper = ends[i + 1]       # end of the NEXT beat's span
        j: int | None = None
        if want == last or want == upper or want >= n:
            # An empty-dialogue span on one side (allowed on silent broll
            # beats) or a boundary at the script edge — nothing to snap.
            j = want
        elif _legal(want):
            j = want
        else:
            best: tuple[int, int] | None = None
            for cand in range(last + 1, upper):
                if not _legal(cand):
                    continue
                stop = cand       # trailing whitespace joins the earlier beat
                while stop < n and script[stop].isspace():
                    stop += 1
                if not (last < stop < upper):
                    continue      # would leave a neighbouring span empty
                d = abs(cand - want)
                if best is None or d < best[0]:
                    best = (d, stop)
            if best is not None:
                j = best[1]
                changed = True
        if j is None:
            groups[-1].append(speech_idx[i + 1])  # merge — no interior edge
            changed = True
        else:
            cuts.append(j)
            last = j
            groups.append([speech_idx[i + 1]])
    if not changed:
        return

    drop: set[int] = set()
    for g, s, e in zip(groups, [0] + cuts, cuts + [n]):
        members = [beats[k] for k in g]
        keep = max(members, key=lambda b: len(
            _AUDIO_TAG_RE.sub("", b.dialogue_text or "").split()))
        keep.ad_section = (members[-1].ad_section if e == n
                           else members[0].ad_section)
        keep.dialogue_text = script[s:e]
        drop.update(id(b) for b in members if b is not keep)
    if drop:
        beats[:] = [b for b in beats if id(b) not in drop]
    for pos, b in enumerate(beats):
        b.index = pos


# Skin-finish phrasing per cast skin_tone (2026-07-31). The old female branch
# hardcoded ONE string ("naturally dewy and BEAUTIFUL") for every woman
# regardless of the cast skin_tone — Nano Banana converged on a single skin
# rendering and every female anchor looked the same. Each entry stays
# FLATTERING (clear healthy attractive skin — never pore/oil emphasis, per the
# influencer-skin rule) while actually naming the cast tone so the render
# follows the casting.
_SKIN_TONE_LOOK: dict[str, str] = {
    "very_fair_warm": "very fair warm-toned, naturally luminous and BEAUTIFUL",
    "fair_golden": "fair golden-toned, naturally radiant and BEAUTIFUL",
    "medium_wheatish": "medium wheatish-toned with a healthy natural glow, "
                       "BEAUTIFUL",
    "dusky_warm": "dusky warm-toned with a rich natural glow, BEAUTIFUL",
    "deep_brown_rich": "deep brown, rich and even-toned with a natural "
                       "luminous glow, BEAUTIFUL",
}


def _skin_look(gender: str | None, skin_tone: str | None = None) -> str:
    """Skin-finish phrasing for image prompts, derived from the CAST skin_tone
    (fixes every-woman-renders-identical). Gender still gates the finish:
    'dewy/glow' reads flattering on women but OILY / SWEATY on a young man — a
    real defect we shipped (a 19yo male creator came out shiny/sweaty). Men
    get a clean matte finish; the anchor image is what Seedance locks onto, so
    the sheen must not be baked in here."""
    if (gender or "").lower().startswith("f"):
        return _SKIN_TONE_LOOK.get(
            (skin_tone or "").lower(), "naturally dewy and BEAUTIFUL")
    return ("fresh, clean and matte with a healthy but NON-shiny finish "
            "(never oily, greasy or sweaty)")


def _validate_timeline(tl: "VideoTimeline", *,
                       max_words: int | None = None,
                       expected_script: str | None = None) -> None:
    """Structural guard — raises ValueError (→ _with_retry retries) on drift.

    ``expected_script``: the client's exact script as extracted from the brief
    (BriefDirectives.explicit_script). When given, script_source is FORCED to
    'verbatim' and full_script must equal it in normal form (_script_norm:
    tags + parenthetical directions ignored, whitespace collapsed) — the check
    the planner's own self-classification never had, which is how user scripts
    got silently paraphrased (2026-07-31).

    Enforces the ad anatomy (hook first, cta last & exactly one CTA run,
    non-decreasing section order), that SPEECH beats exactly partition the
    script, that no talking_head beat carries a mouth-occupying action (breaks
    lipsync) / a face-obstructing accessory / illogical hand staging, that no
    beat is over-written (>_MAX_BEAT_WORDS spoken words), that no spoken
    talking_head beat is under-written (<_MIN_TALKING_HEAD_WORDS spoken words
    misses Seedance's 2s reference-audio minimum), that no dialogue_text ends
    on an [audio tag] (its audio leaks into the next beat's slice), and — when
    ``max_words`` is given — that the whole script fits the duration's word
    budget (a 30s target spoken at real pacing can't absorb a 77-word script;
    a measured run came out 44% over).

    Reaction beats (beat_kind="reaction") are silent screen-time holds: they
    MUST carry an empty dialogue_text and a positive hold_s, and they are
    EXCLUDED from the script-partition check and from the phrase / word-count
    rules (they contribute no spoken words).
    """
    if not tl.beats:
        raise ValueError("timeline has no beats")
    if expected_script is not None:
        # The extractor found a client-written script — verbatim is not the
        # planner's call anymore. Forcing the flag also keeps the word-budget
        # and beat-cap exemptions consistent (a misclassified user script used
        # to fall into the budget-retry loop, which SHORTENS it).
        tl.script_source = "verbatim"
        got, want = _script_norm(tl.full_script), _script_norm(expected_script)
        if got != want:
            k = next((i for i in range(min(len(got), len(want)))
                      if got[i] != want[i]), min(len(got), len(want)))
            raise ValueError(
                "full_script must be the brief's script COPIED WORD-FOR-WORD "
                "(script_source='verbatim'). First divergence at char "
                f"{k}: you wrote ...{got[max(0, k - 30):k + 30]!r}..., the "
                f"brief's script has ...{want[max(0, k - 30):k + 30]!r}.... "
                "Re-emit with the script copied exactly — no paraphrasing, "
                "reordering, shortening, or added/dropped words."
            )
    # A reaction beat is silent screen time — empty dialogue, positive hold.
    for b in tl.beats:
        if b.beat_kind != "reaction":
            continue
        if (b.dialogue_text or "").strip():
            raise ValueError(
                f"beat {b.index} is a reaction (silent hold) but carries "
                f"dialogue_text {b.dialogue_text.strip()[:40]!r} — a reaction "
                f"beat speaks NO words; leave dialogue_text empty."
            )
        if not b.hold_s or b.hold_s <= 0:
            raise ValueError(
                f"beat {b.index} is a reaction beat but has no positive hold_s "
                f"— set hold_s to the silent screen time in seconds (e.g. 0.8)."
            )
    # voice_mode / dialogue_text consistency: when the planner EXPLICITLY sets
    # voice_mode, the text must agree — a "silent" beat with spoken text (or an
    # on_camera/background_vo beat with NO text) produces a contradictory state
    # that causes pipeline and assembly to disagree and crash the paid run.
    # Only validated when voice_mode is explicitly set (not None — None is
    # derived downstream).
    for b in tl.beats:
        explicit_vm = getattr(b, "voice_mode", None)
        if explicit_vm is None:
            continue
        has_text = bool((b.dialogue_text or "").strip())
        if explicit_vm == "silent" and has_text:
            raise ValueError(
                f"beat {b.index} has voice_mode='silent' but carries non-empty "
                f"dialogue_text {b.dialogue_text.strip()[:40]!r} — a silent beat "
                f"must have an empty dialogue_text; either clear the text or "
                f"change voice_mode to 'on_camera' / 'background_vo'."
            )
        if explicit_vm in ("on_camera", "background_vo") and not has_text:
            raise ValueError(
                f"beat {b.index} has voice_mode={explicit_vm!r} but empty "
                f"dialogue_text — a speaking beat must carry the spoken line; "
                f"either add the dialogue or set voice_mode='silent'."
            )
    # Mouth-occupying action on a lip-synced beat → warped/double mouth;
    # a face-obstructing accessory hides the face the lipsync needs; illogical
    # hand staging produces phantom hands. Reaction beats are not lip-synced.
    for b in tl.beats:
        if b.shot_type != "talking_head" or b.beat_kind == "reaction":
            continue
        note = f"{b.visual_note or ''} {b.broll_shot or ''}"
        if _MOUTH_ACTION_RE.search(note):
            raise ValueError(
                f"beat {b.index} is a talking_head with a mouth-occupying "
                f"action (breaks lipsync): {note.strip()[:60]!r}. Route it to "
                f"a broll beat (hands_using/demo_steps/texture_pour)."
            )
        if _FACE_OBSTRUCT_RE.search(note):
            raise ValueError(
                f"beat {b.index} is a talking_head whose face is obscured "
                f"(sunglasses / hat brim over the eyes): {note.strip()[:60]!r}. "
                f"Lipsync needs the full face — push eyewear up onto the head "
                f"and keep the face unobstructed, or make this a broll beat."
            )
    # Physically-illogical hand staging (any beat) — phantom/extra hands and
    # caressing read as a deformity to the image+video models.
    for b in tl.beats:
        note = f"{b.visual_note or ''} {b.broll_shot or ''}"
        if _ILLOGICAL_HANDS_RE.search(note):
            raise ValueError(
                f"beat {b.index} stages physically-illogical hands "
                f"({note.strip()[:60]!r}) — exactly ONE pair of hands belongs "
                f"to ONE person; no caressing / extra / phantom hands. Restage "
                f"with a single pair of hands holding the product."
            )
    # Dialogue must EXACTLY partition the script. We assert concatenation
    # equality rather than per-beat substring search: a repeated short slice
    # ("so good", "I know") could otherwise bind to the WRONG occurrence and
    # silently drift every downstream cut (beat_windows reads each window by
    # position in the alignment). Exact partition is both stronger and
    # collision-proof. Reaction beats are silent (empty dialogue) and excluded.
    speech_beats = [b for b in tl.beats if b.beat_kind != "reaction"]
    joined = "".join(b.dialogue_text or "" for b in speech_beats)
    if joined != tl.full_script:
        # A whitespace-only slip (dropped inter-beat spaces) is repaired
        # deterministically — re-slice full_script onto the beats so they
        # concatenate exactly, no re-roll needed.
        if _repartition_onto_full_script(tl.full_script, speech_beats):
            joined = "".join(b.dialogue_text or "" for b in speech_beats)
    if joined != tl.full_script:
        # Genuine content divergence — point the planner at the exact spot so
        # the retry is surgical, not a blind re-roll.
        k = next((i for i in range(min(len(joined), len(tl.full_script)))
                  if joined[i] != tl.full_script[i]),
                 min(len(joined), len(tl.full_script)))
        raise ValueError(
            "beat dialogue_text must concatenate to full_script EXACTLY "
            "(same words/punctuation, in index order); got "
            f"{len(joined)} chars vs {len(tl.full_script)} in full_script. "
            f"First divergence at char {k}: beats have "
            f"{joined[max(0, k - 25):k + 15]!r}, full_script has "
            f"{tl.full_script[max(0, k - 25):k + 15]!r}. Re-emit beats whose "
            "dialogue_text concatenate to full_script with no gaps, overlaps, "
            "or dropped/added words."
        )
    # The partition is now exact — deterministically snap any mid-sentence
    # boundary to the nearest legal phrase edge (merging beats that straddle
    # an unsplittable phrase-unit) instead of re-rolling the planner. Runs
    # BEFORE the anatomy checks so structure is judged on the beats that
    # actually ship. The phrase-boundary loop below stays as a backstop.
    _snap_boundaries_to_phrase_edges(tl)
    speech_beats = [b for b in tl.beats if b.beat_kind != "reaction"]
    sections = [b.ad_section for b in tl.beats]
    if sections[0] != "hook":
        raise ValueError(f"first beat must be 'hook', got '{sections[0]}'")
    if sections[-1] != "cta":
        raise ValueError(f"last beat must be 'cta', got '{sections[-1]}'")
    # Sections must be non-decreasing in anatomy order (solution↔proof may
    # interleave — see _SECTION_RANK).
    ranks = [_SECTION_RANK[s] for s in sections]
    if any(b < a for a, b in zip(ranks, ranks[1:])):
        raise ValueError(
            f"ad_sections out of order: {sections} — beats must run "
            "hook → problem → (solution/proof, freely interleaved) → cta; a "
            "'problem' beat may not follow a solution/proof beat, and cta must "
            "be last."
        )
    # Exactly one CTA run, at the end.
    first_cta = sections.index("cta")
    if any(s != "cta" for s in sections[first_cta:]):
        raise ValueError("cta beats must be contiguous at the end")
    # Phrase-aligned boundaries — a cut may only land at a NATURAL break so a
    # sentence never splits across two beats (the hook "Men, if you're trying
    # to impress a woman — here's a small tip" must NOT straddle a cut). For
    # each boundary between beat i and beat i+1, beat i's spoken text (audio
    # tags stripped, trailing spaces stripped) must end on terminal punctuation
    # or an em-dash, OR the boundary must coincide with a newline in
    # full_script. The last beat ends the script and is exempt. Verbatim lines
    # that don't punctuate every line break at a newline and are still valid.
    offset = 0
    for i, b in enumerate(speech_beats):
        offset += len(b.dialogue_text or "")
        if i == len(speech_beats) - 1:
            break  # last speech beat ends the script
        ends_at_newline = (
            (b.dialogue_text or "").endswith("\n")
            or (offset < len(tl.full_script) and tl.full_script[offset] == "\n")
        )
        if ends_at_newline:
            continue
        spoken = _AUDIO_TAG_RE.sub("", b.dialogue_text or "").rstrip(" ")
        if not _ends_at_phrase_boundary(spoken):
            nxt = speech_beats[i + 1]
            nxt_spoken = _AUDIO_TAG_RE.sub("", nxt.dialogue_text or "").lstrip()
            raise ValueError(
                f"beat {b.index} ends mid-sentence ({spoken[-30:]!r}) and the "
                f"sentence continues in beat {nxt.index} "
                f"({nxt_spoken[:30]!r}) — a cut may only land at a phrase "
                f"boundary. Put the WHOLE sentence in ONE beat (merge it, or "
                f"move the boundary to the sentence end); a beat may end only "
                f"at a sentence end (. ? ! …), a colon/semicolon, a dash, or "
                f"a line break. If the merged sentence is a single "
                f"unsplittable sentence, one beat carrying it is ACCEPTED "
                f"even past the soft word cap."
            )
    # Per-beat dialogue rules — the fast-cut word cap, the Seedance
    # reference-audio word floor, and tag placement. Spoken words only
    # (audio tags stripped). Reaction beats carry no speech and are exempt.
    held_used = False  # the ONE earned hold per video (≤_MAX_HELD_BEAT_WORDS)
    for b in speech_beats:
        text = b.dialogue_text or ""
        n_words = len(_AUDIO_TAG_RE.sub("", text).split())
        if n_words > _MAX_BEAT_WORDS:
            spoken = _AUDIO_TAG_RE.sub("", text).rstrip(" ")
            is_verbatim = getattr(tl, "script_source", "generated") == "verbatim"
            if _has_interior_phrase_boundary(spoken):
                # Splittable — there IS an interior phrase boundary to cut on.
                # The first over-cap beat within the held ceiling is the one
                # earned hold; every other over-cap beat must split.
                if not held_used and n_words <= _MAX_HELD_BEAT_WORDS:
                    held_used = True
                else:
                    raise ValueError(
                        f"beat {b.index} has {n_words} spoken words — past "
                        f"the fast-cut ceiling (max {_MAX_BEAT_WORDS}; ONE "
                        f"held beat per video may carry up to "
                        f"{_MAX_HELD_BEAT_WORDS}). SPLIT it at an interior "
                        f"phrase boundary into a talking_head beat + a B-roll "
                        f"cutaway (or two beats) so each carries "
                        f"≤{_MAX_BEAT_WORDS} words."
                    )
            elif not is_verbatim and n_words > _HARD_MAX_BEAT_WORDS:
                # A single atomic sentence too long even for one beat; a
                # generated line can be reworded (verbatim copy is exempt).
                raise ValueError(
                    f"beat {b.index} is a single {n_words}-word sentence with "
                    f"no interior phrase boundary to cut on, past the hard "
                    f"{_HARD_MAX_BEAT_WORDS}-word ceiling — shorten/reword the "
                    f"sentence, or add a natural break so it can be split."
                )
            # else: an unsplittable sentence within the ceiling (or verbatim
            # copy) — the soft cap yields; split_long_windows chops the visual.
        if (b.shot_type == "talking_head" and text.strip()
                and n_words < _MIN_TALKING_HEAD_WORDS):
            raise ValueError(
                f"beat {b.index} is a talking_head with only {n_words} spoken "
                f"words — under the {_MIN_TALKING_HEAD_WORDS}-word minimum "
                f"(Seedance reference audio needs ≥2s of speech). MERGE the "
                f"line into an adjacent beat, or make this beat broll so the "
                f"VO carries it."
            )
        if _TRAILING_TAG_RE.search(text):
            raise ValueError(
                f"beat {b.index} dialogue_text ends with an audio tag "
                f"({text.strip()[-25:]!r}) — a trailing tag's audio leaks "
                f"into the NEXT beat's slice. A tag may only OPEN a line: "
                f"place it immediately BEFORE the words it modifies."
            )
    # Slow-pace vocabulary in voice_direction is a computable defect with a
    # computable fix — substitute "brisk" in place (deterministic repair,
    # project rule) rather than re-rolling the whole plan over one adverb.
    for b in tl.beats:
        vd = getattr(b, "voice_direction", "") or ""
        if vd and _SLOW_PACE_RE.search(vd):
            b.voice_direction = _SLOW_PACE_RE.sub("brisk", vd)
    # A broll beat with broll_type=None fell through EVERY reference branch in
    # pipeline._anchor_refs — with shows_creator=False its anchor rendered from
    # TEXT ALONE, which is a guaranteed product/scene hallucination. Computable
    # defect, computable repair: product-ish text → product_in_scene (gets the
    # product reference), otherwise lifestyle.
    for b in tl.beats:
        if b.shot_type == "broll" and not getattr(b, "broll_type", None):
            text = f"{b.broll_shot or ''} {b.visual_note or ''}"
            b.broll_type = (
                "product_in_scene" if _PRODUCT_WORD_RE.search(text)
                else "lifestyle"
            )
    # Framing variety (2026-07-31): the same framing on two CONSECUTIVE
    # talking_head beats is a computable defect with a computable fix — swap
    # the repeat to a safe framing (front-cam, no hand-slot cost, no special
    # product/mirror wiring) that differs from both neighbours. Content-bound
    # framings (product pivot, mirror, grwm) are never swapped away — changing
    # them would change WHAT the shot shows, not just how it's framed.
    _no_swap = {"product_reveal_pivot", "mirror", "grwm"}
    prev_framing: str | None = None
    for i, b in enumerate(tl.beats):
        if b.shot_type != "talking_head":
            prev_framing = None   # a cutaway breaks adjacency (jump-back is fine)
            continue
        f = getattr(b, "framing", None)
        if f and f == prev_framing and f not in _no_swap:
            nxt = next(
                (getattr(nb, "framing", None) for nb in tl.beats[i + 1:]
                 if nb.shot_type == "talking_head"), None,
            )
            b.framing = next(
                (cand for cand in _VARIETY_SAFE_FRAMINGS
                 if cand != prev_framing and cand != nxt),
                _VARIETY_SAFE_FRAMINGS[0],
            )
        prev_framing = b.framing
    # Total word budget — bounds the VO (and therefore the video) duration.
    # ONLY enforced for a GENERATED script with a forced target; a verbatim
    # script's length is the creative team's call (Mode A), and an AI-decided
    # duration is self-consistent, so neither gets capped.
    if max_words is not None and getattr(tl, "script_source", "generated") != "verbatim":
        total = len(_AUDIO_TAG_RE.sub("", tl.full_script).split())
        if total > max_words:
            raise ValueError(
                f"full_script is {total} spoken words — over the "
                f"{max_words}-word budget for target_duration_s="
                f"{tl.target_duration_s}. Cut filler and tighten lines until "
                f"the script is ≤{max_words} words; fill runtime with B-roll, "
                f"never slower talking."
            )


class ChunkSpec(BaseModel):
    index: int
    t_start: float
    t_end: float
    dialogue: str
    action: str = Field(
        description="What's visible at the OPENING of this chunk (t_start). "
                    "Used to render the chunk's first-frame anchor in "
                    "Nano Banana.",
    )
    closing_action: str = Field(
        default="",
        description="What's visible at the CLOSING of this chunk (t_end). "
                    "Used to render the chunk's last-frame anchor in "
                    "Nano Banana for FLF2V chaining. For chunks i<N-1, this "
                    "should match the next chunk's opening action for "
                    "seamless chaining. For the last chunk, this is the "
                    "final shot of the whole video.",
    )
    summary: str = Field(description="One-line summary used to inform the next chunk.")
    framing: FramingChoice = Field(
        default="selfie",
        description="Camera framing for this chunk, copied from the screenplay.",
    )


# ── VoicePersona ────────────────────────────────────────────────────────────
# Per-character speaking-voice spec for native Seedance audio generation.
# Accent + multilingual identity are FIXED in render_voice_persona (not
# LLM-chosen) so every clip steers the same polished Indian-English voice.

class VoicePersona(BaseModel):
    gender: str = Field(description="male or female — match the character.")
    age_band: str = Field(description="e.g. 'early-20s', 'mid-30s'.")
    timbre: str = Field(description="2-4 words: e.g. 'warm bright', "
                                    "'soft husky', 'clear crisp'.")
    pace: str = Field(description="e.g. 'calm conversational', 'punchy upbeat'.")
    warmth: str = Field(description="e.g. 'friendly', 'confident', 'soothing'.")


def render_voice_persona(p: "VoicePersona", *,
                         include_pace: bool = False) -> str:
    """Compact clause injected into every speaking-beat video prompt.

    Accent + multilingual identity are FIXED here (not LLM-chosen) so every
    clip steers the same polished Indian-English voice; the anti-stereotype
    guard keeps the model off a sing-song / exaggerated accent. PACE is
    omitted by default — the LLM's free-text pace (e.g. "slow deliberate") made
    the first native run drag; cadence is owned by the brisk-real-time clause in
    the pipeline's native prompt instead.

    ``include_pace=True`` is for the generator that authors the whole audio
    track from this one clause and has no separate cadence direction to inherit:
    there, dropping pace leaves delivery entirely to the model.
    """
    pace = f", with a {p.pace} delivery" if include_pace else ""
    return (
        f"The voice is a {p.warmth}, {p.timbre} {p.gender} voice, {p.age_band}"
        f"{pace}, speaking in a polished, neutral, urban Indian-English accent "
        "(the same refined accent in Hindi when the line is Hindi) — NOT an "
        "exaggerated or stereotypical accent."
    )


class VoiceMatch(BaseModel):
    """Result of matching a cloned voice to a cast character."""
    voice_id: str | None = Field(
        default=None,
        description="The chosen cloned voice's id, or null when no candidate "
                    "genuinely fits the character (caller falls back to the "
                    "curated catalog).",
    )
    reason: str = Field(description="One-sentence justification for the pick "
                                    "(or for returning null).")


# ── Indian-creator variation enums ─────────────────────────────────────────
# Constrains Gemini's CharacterDescription output to a defined palette so the
# character image skill MD can interpolate predictably. Adding/removing values here
# requires updating the matching menus in docs/character-image-skill.md.

SkinTone = Literal[
    "very_fair_warm",        # Kashmiri / Punjabi range, pale-honey
    "fair_golden",           # Urban Mumbai/Delhi creator range, light wheatish
    "medium_wheatish",       # UP/Bengal/Bihar range, warm neutral
    "medium_warm_brown",     # Maharashtra/Gujarat range, healthy radiance
    "deep_brown_rich",       # Tamil/Kerala/Karnataka range, rich deep brown
]

Region = Literal[
    "north_indian",
    "south_indian",
    "northeast_indian",
    "west_indian",
    "east_indian",
]

HairStyle = Literal[
    # Female
    "long_loose_natural_waves",
    "long_sleek_straight",
    "messy_high_bun",
    "low_loose_bun",
    "high_ponytail",
    "low_side_ponytail",
    "half_up_half_down",
    "single_loose_braid",
    "short_modern_bob",
    # Male
    "men_short_tousled",
    "men_buzz_short",
    "men_side_part_classic",
    "men_medium_textured",
]

Setting = Literal[
    "apartment_kitchen_counter",
    "apartment_kitchen_dining",
    "apartment_living_couch",
    "apartment_balcony_natural_light",
    "bedroom_desk_warm",
    "cafe_window_seat",
    "gym_corner_yoga_mat",
    "bathroom_skincare_mirror",
    "outdoor_street_natural_light",
]

Expression = Literal[
    "candid_mid_laugh",
    "calm_mid_sentence",
    "tasting_reaction",
    "focused_engaged",
    "surprised_delighted",
    "soft_warm_smile",
    "thoughtful_listening",
]


class CharacterDescription(BaseModel):
    age: int
    gender: str
    ethnicity: str
    # Structured variation fields — drive the menus in
    # docs/character-image-skill.md. NEVER default to a single value across
    # briefs; the goal of these enums is real variation per brief.
    region: Region = Field(
        description="Indian regional cluster. Vary based on brief audience; "
                    "do not default to one region.",
    )
    skin_tone: SkinTone = Field(
        description="Indian skin-tone bucket. Indian creators span the full "
                    "range — DO NOT default to a single value across briefs. "
                    "Match to the brand demographic implied by the brief.",
    )
    hair_style: HairStyle = Field(
        description="Specific hairstyle from the menu. Vary across briefs — "
                    "do NOT default to messy_high_bun.",
    )
    setting: Setting = Field(
        description="Where the character is shot. Pick what fits the product "
                    "category and brief energy.",
    )
    expression: Expression = Field(
        description="Default expression for the character reference still. "
                    "For first-frame, may be overridden by opening action.",
    )
    # Free-text fields — elaborate on the structured choices above.
    hair: str = Field(
        description="Natural-language hair elaboration that extends "
                    "hair_style with colour, length, texture specifics.",
    )
    face: str = Field(
        description="Face specifics beyond skin_tone — eye shape, brow, lip, "
                    "septum/nose-ring details. AFFIRMATIVE language only.",
    )
    wardrobe: str
    energy: str
    voice_vibe: "VoiceVibe" = Field(  # type: ignore[name-defined]
        description="Voice vibe for the ElevenLabs VO. Pick the one that "
                    "fits the brief energy and the character's archetype: "
                    "upbeat_lively (fun social-media reels — the default for "
                    "energetic UGC); warm_relatable (BFF/casual — story / "
                    "testimonial); sassy_expressive (humor/sass — gossip "
                    "reels / controversial hooks); youthful_excited (beauty / "
                    "skincare / fashion punch); professional_clear (tech / "
                    "finance / explainer authority); soothing_narrative "
                    "(calm wellness / storytime / day-in-life); "
                    "crisp_informative (crisp, precise how-to / demo / data "
                    "clarity); mature_authentic (mature, grounded, "
                    "middle-aged testimonial / voice-of-experience). The "
                    "pipeline maps (gender, vibe) → concrete voice_id from the "
                    "curated catalog in app/voice.py.",
    )
    voice_persona: VoicePersona = Field(
        description="The character's speaking voice for native Seedance audio: "
                    "gender (match character), age_band, timbre, pace, warmth. "
                    "Accent is ALWAYS polished neutral urban Indian-English "
                    "(EN+HI) — do not specify accent here; just the personal "
                    "voice qualities that fit the archetype.",
    )


class ProductInFirstFrameDecision(BaseModel):
    """Structured-output schema for should_open_with_product."""
    in_first_frame: bool = Field(
        description="True if the product should be visible in the opening "
                    "frame; False if the opening shot should feature only "
                    "the creator and the product is introduced later.",
    )
    reasoning: str = Field(
        description="One short sentence justifying the decision — primarily "
                    "for debugging and so the model 'thinks' before "
                    "committing.",
    )


# ---------- Client ----------

_MOTION_MODE_TASK = {
    "sync_talk": (
        "MODE: talking-head, lipsynced by a downstream pass. Direct continuous "
        "GENTLE talking motion — subtle, low-amplitude mouth movement through "
        "the WHOLE clip, natural blinks; never exaggerated jaw flapping (the "
        "downstream pass re-renders the lips, and big source jaw swings bleed "
        "through as mismatched articulation) — and any gesture must BEGIN and "
        "RESOLVE within {window_len:.1f}s. Do NOT write the spoken line; the "
        "mouth is corrected downstream."
    ),
    "native_talk": (
        "MODE: talking-head, audio-driven. Put the spoken line in double quotes "
        "and state that her voice and lip movements follow Audio 1. The reference "
        "audio paces the take — no gesture-window budget. Line: \"{line}\"."
    ),
    "broll": (
        "MODE: b-roll cutaway (voiceover continues underneath). Direct ONE "
        "subject action and ONE camera move — both QUICK and decisive: real "
        "UGC cuts fast, so the action plays at real-time energy, never slow, "
        "creeping, or drawn out, and it is fine to cut mid-motion. "
        "Product/hands shots use exactly one quick, smooth push-in (a clean "
        "scale only — never orbit, pan, or parallax: a wandering camera over "
        "a product is the #1 morph source)."
    ),
    # ── Omni modes (gemini-omni-flash-preview authors video AND audio) ──
    # The persona clause arrives pre-rendered and MUST survive verbatim — it is
    # the only thing holding one voice identity across the run's clips.
    "omni_talk": (
        "MODE: Omni talking-head — the model generates the creator SAYING the "
        "line out loud (video + voice in one pass). Put the spoken line in "
        "double quotes exactly as given (its respellings are deliberate) and "
        "wrap it in the voice description. Line: \"{line}\".\n"
        "Voice (include VERBATIM, never paraphrase): {persona_clause}\n"
        "End the audio direction with: \"No background music, no extra sound "
        "effects.\""
    ),
    "omni_vo_broll": (
        "MODE: Omni b-roll with voiceover — the model generates the cutaway "
        "visuals AND the creator's voice continuing off-camera. Direct the "
        "b-roll action, then state that the same voice continues off-camera as "
        "a voiceover and put the spoken line in double quotes exactly as given "
        "(its respellings are deliberate). Line: \"{line}\".\n"
        "Voice (include VERBATIM, never paraphrase): {persona_clause}\n"
        "End the audio direction with: \"No background music, no extra sound "
        "effects.\""
    ),
    "omni_silent": (
        "MODE: Omni silent b-roll. No dialogue. Name the natural ambient sound "
        "of the scene (room tone, fabric, handling noise) and close the audio "
        "direction with: \"No dialogue, no music.\""
    ),
}

# The seedance_native + heygen_replicate engines classify beats with the
# voice-mode taxonomy (on_camera / background_vo / silent); older engines pass
# the task keys directly. Normalize both onto the three motion task templates:
# on_camera is a downstream-lipsynced talking head (sync_talk), and both
# background_vo and silent are cutaways (broll). Without this, heygen_replicate
# beats KeyError'd on the raw voice-mode name and silently fell back to the
# deterministic f-string, disabling Opus prompt-writing for the whole engine.
_MOTION_MODE_TASK["omni_multishot"] = (
    "MODE: Omni MULTI-SHOT take — this ONE generation contains {n_shots} "
    "different shots, hard-cut at fixed times, with the dialogue running "
    "continuously across the cuts.\n"
    "OVERRIDE the single-unbroken-shot lock from the system instruction — it "
    "does NOT apply to this beat. Never write \"in a single unbroken continuous "
    "shot\" or \"no scene cuts\" here: this clip MUST cut.\n"
    "Write the visual section as a TIMECODE SHOT LIST — one entry per shot, in "
    "this exact order, with these exact time ranges, each rendering the scene "
    "given for it (this is the screenplay; do not substitute your own):\n"
    "{shot_list}\n"
    "Keep each entry's LABEL. For a shot labelled INSERT CUTAWAY you must also "
    "state that her face, mouth and head are OUT OF FRAME for that whole shot "
    "and that nobody is seen talking — naming the exclusion is what stops the "
    "model quietly rendering another talking-to-lens angle instead (measured). "
    "Require hard cuts at the given times with no dissolve, wipe or transition "
    "effect; the same creator, wardrobe and grade throughout; and nothing added "
    "that is not on the list.\n"
    "Audio: ONE unbroken take of speech across all {n_shots} shots that never "
    "restarts, pauses or repeats. Put the line in double quotes exactly as "
    "given: \"{line}\". Then say which shots she is ON CAMERA speaking in, and "
    "that over every INSERT CUTAWAY her voice CONTINUES as an off-camera "
    "voiceover while she is not visible.\n"
    "Voice (include VERBATIM, never paraphrase): {persona_clause}\n"
    "End the audio direction with: \"No background music, no extra sound "
    "effects.\""
)

_MODE_TO_TASK = {
    "sync_talk": "sync_talk",
    "native_talk": "native_talk",
    "broll": "broll",
    "on_camera": "sync_talk",
    "background_vo": "broll",
    "silent": "broll",
    # Omni mode reuses the voice-mode taxonomy but with its own task templates
    # AND its own skill block — the prompt style is inverted vs Seedance
    # (full narrative brief, not defensive motion-only). The pipeline passes
    # modes already prefixed (omni_talk / omni_vo_broll / omni_silent).
    "omni_talk": "omni_talk",
    "omni_vo_broll": "omni_vo_broll",
    "omni_silent": "omni_silent",
    # A folded multi-shot take (pipeline._omni_group_shots) — several screenplay
    # beats in ONE generation.
    "omni_multishot": "omni_multishot",
}


class GeminiClient:
    def __init__(self, settings: Settings, *,
                 skill_md_path: "Path | str | None" = None,
                 image_skill_md_path: "Path | str | None" = None,
                 omni_skill_md_path: "Path | str | None" = None,
                 llm_provider: str | None = None,
                 _client: Any = None,
                 _claude_client: Any = None):
        self._provider = llm_provider or settings.llm_provider
        if self._provider not in ("claude", "gemini"):
            raise ValueError(
                f"unknown llm_provider {self._provider!r} — "
                f"expected 'claude' or 'gemini'"
            )
        self._claude = None
        if self._provider == "claude":
            from app.claude import ClaudeBackend
            self._claude = ClaudeBackend(settings, _client=_claude_client)
            self._client = _client
        else:
            if not settings.gemini_api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. Add it to .env (see .env.example)."
                )
            if _client is not None:
                self._client = _client
            else:
                from google import genai
                self._client = genai.Client(api_key=settings.gemini_api_key)
        self._skill_md_path = skill_md_path
        self._skill_md_cache: str | None = None
        self._image_skill_md_path = image_skill_md_path
        self._image_skill_md_cache: str | None = None
        self._omni_skill_md_path = omni_skill_md_path
        self._omni_skill_md_cache: str | None = None

    def _config(self, *, schema=None,
                system_instruction: "str | list[str] | None" = None,
                temperature: float = 0.4):
        """Build a GenerateContentConfig.

        We deliberately DO NOT set max_output_tokens. Truncation at fixed
        caps (4k / 8k) caused JSON to be cut mid-string in split_script and
        chain-of-thought to leak in the image-prompt calls. Letting the
        SDK use each model's full default capacity is safer.
        """
        from google.genai import types
        # Gemini's system_instruction is a plain string; the multi-block
        # (per-prefix cache_control) form is claude-only, so flatten a list
        # back to one string here.
        if isinstance(system_instruction, list):
            system_instruction = "\n\n".join(system_instruction)
        return types.GenerateContentConfig(
            response_mime_type="application/json" if schema is not None else "text/plain",
            response_schema=schema,
            system_instruction=system_instruction,
            temperature=temperature,
        )

    async def _generate(self, *, tier: str, prompt: str, schema=None,
                        system_instruction: "str | list[str] | None" = None,
                        temperature: float = 0.4,
                        image_url: str | None = None) -> Any:
        """Provider seam for every text task.

        tier: "pro" (creative core), "fast" (workhorse), "lite" (cheap
        binary decisions — rides the fast model on Gemini, which has no
        cheaper text tier). With a schema, returns the validated Pydantic
        object; without one, returns the raw text.

        image_url: claude-only multimodal grounding (anchor frame). The
        gemini branch ignores it — the motion-prompt feature runs under the
        claude provider, with an f-string fallback otherwise.
        """
        if self._provider == "claude":
            return await self._claude.generate(
                tier=tier, prompt=prompt, schema=schema,
                system_instruction=system_instruction,
                temperature=temperature, image_url=image_url,
            )
        model = GEMINI_PRO_MODEL if tier == "pro" else GEMINI_FAST_MODEL
        resp = await self._client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=self._config(
                schema=schema, system_instruction=system_instruction,
                temperature=temperature,
            ),
        )
        costs.record_gemini_text(model, getattr(resp, "usage_metadata", None))
        if schema is None:
            return resp.text
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            return schema.model_validate_json(resp.text)
        # Non-BaseModel schemas (e.g. list[ChunkSpec]).
        return TypeAdapter(schema).validate_json(resp.text)

    async def _with_retry(self, fn, *, max_attempts: int = 3,
                          delay_base: float = 1.5,
                          label: str = "gemini call") -> Any:
        """Run an async callable up to max_attempts times with exponential
        backoff. Retries on JSON parse errors, Pydantic validation errors,
        network errors, and transient API errors. Auth and quota errors
        fail fast.

        Why: Gemini Flash occasionally returns malformed JSON (unterminated
        strings, missing brackets) under load — retrying with the same
        prompt usually succeeds within 2 attempts. Network blips do the
        same.

        Corrective feedback: if ``fn`` accepts one argument, the previous
        attempt's exception is passed to it (None on the first attempt) so it
        can tell the model exactly what to fix. A blind re-roll of a
        structural rejection can degrade into a worse sample (e.g. the planner
        collapsing to an empty timeline); a targeted "fix THIS beat" retry
        corrects it in one shot. Zero-arg callables are called unchanged.
        """
        import httpx
        import inspect

        transient_excs: tuple[type[Exception], ...] = (
            ValueError, ValidationError,
            ConnectionError, TimeoutError,
            httpx.RequestError,
        )
        try:
            # Claude-path transients. Lazy + guarded: gemini-only installs
            # may not have the anthropic SDK and must not break here.
            import anthropic
        except ImportError:
            pass
        else:
            transient_excs += (
                anthropic.APIConnectionError, anthropic.RateLimitError,
                anthropic.InternalServerError, anthropic.OverloadedError,
            )

        wants_feedback = len(inspect.signature(fn).parameters) >= 1
        last_error: Exception | None = None
        feedback: Exception | None = None
        # Distinct rejection messages across attempts, oldest first. Feedback
        # must carry ALL of them, not just the last: a live run ping-ponged
        # (mid-sentence → empty-beats → the IDENTICAL mid-sentence split)
        # because attempt 3's correction only mentioned the empty-beats error
        # and the model re-made the mistake attempt 1 had already rejected.
        corrections: list[str] = []
        for attempt in range(1, max_attempts + 1):
            try:
                return await (fn(feedback) if wants_feedback else fn())
            except transient_excs as e:
                # ValueError covers json.JSONDecodeError AND the structural
                # ValueErrors raised by _validate_timeline — a fresh sample
                # from the model usually fixes both. (Previously a plain
                # ValueError fell through to the generic handler and FAILED
                # FAST, so validator rejections never actually retried.)
                # httpx.RequestError covers ReadError, ConnectError,
                # RemoteProtocolError, ReadTimeout, WriteError — all
                # transient network errors that fire when many parallel
                # SDK calls saturate a connection pool.
                last_error = e
                kind = type(e).__name__
            except Exception as e:
                # Inspect the message for transient-looking strings before
                # deciding to retry vs fail fast.
                msg = str(e).lower()
                transient_hints = (
                    "503", "502", "504", "deadline", "unavailable",
                    "resource_exhausted", "internal", "timeout",
                )
                if not any(h in msg for h in transient_hints):
                    raise
                last_error = e
                kind = type(e).__name__

            msg = str(last_error)
            if msg not in corrections:
                corrections.append(msg)
            feedback = last_error if len(corrections) == 1 else ValueError(
                "prior attempts were rejected for ALL of the following — "
                "fix EVERY one of them together:\n"
                + "\n".join(f"- {m}" for m in corrections)
            )

            if attempt < max_attempts:
                # 503/server-overload errors need real breathing room — 10s+.
                # JSON/validation failures are fast to retry (1-2s is fine).
                is_server_overload = any(
                    h in str(last_error).lower()
                    for h in ("503", "502", "504", "unavailable", "deadline")
                )
                if is_server_overload:
                    wait = min(10.0 * (2 ** (attempt - 1)), 60.0)  # 10s → 20s → 40s → cap 60s
                else:
                    wait = delay_base ** (attempt - 1)             # 1s → 1.5s (original)
                print(
                    f"[{label} retry {attempt}/{max_attempts} in {wait:.1f}s] "
                    f"{kind}: {last_error}",
                    file=sys.stderr, flush=True,
                )
                await asyncio.sleep(wait)
            else:
                # Exhausted — surface the last error.
                assert last_error is not None
                raise last_error
        # Unreachable, satisfies type checker.
        assert last_error is not None
        raise last_error

    async def generate_script(self, *, brief: str, duration_s: int) -> ScriptDraft:
        # Phase 2: 8-second chunks (was 15s). Phase 1: Hinglish lexical
        # approach, voice biography, silent reaction beats, word density
        # 2.5-3 w/s for Indian English (slower than US 3.5+).
        n_chunks = max(1, math.ceil(duration_s / CHUNK_SECONDS))
        words_per_chunk = int(CHUNK_SECONDS * 2.8)
        total_words = words_per_chunk * n_chunks
        prompt = (
            f"You are processing a UGC video ad brief for a real Indian "
            f"Instagram creator (Mumbai/Delhi-based, English-medium "
            f"educated, watches a lot of American/British online content). "
            f"NOT a corporate VO, NOT a news anchor, NOT a Bollywood drama.\n\n"
            f"### MODE DETECTION — READ THE BRIEF CAREFULLY FIRST\n"
            f"Decide which mode the brief is in:\n\n"
            f"**MODE A — VERBATIM SCRIPT** (the brief contains explicit "
            f"spoken dialogue, multiple sentences in creator-voice style, "
            f"possibly with scene directions in parentheses like '(takes a "
            f"bite)' or '(start eating from the bowl)'):\n"
            f"  → SET full_script = the brief, COPIED WORD-FOR-WORD. Keep "
            f"every exclamation mark, capital, em-dash, line break. Keep "
            f"any scene directions in parens exactly as the user wrote them.\n"
            f"  → DO NOT paraphrase. DO NOT add Hindi words. DO NOT inject "
            f"'yaar / matlab / bhai / bas / na' into the script. DO NOT "
            f"reorder benefits. DO NOT shorten or 'tighten' anything.\n"
            f"  → DO NOT 'fix' the script for word density — if the user's "
            f"verbatim script is longer than the duration suggests, the "
            f"creator will just speak slightly faster. That's fine.\n"
            f"  → The Hinglish rules below DO NOT APPLY in Mode A.\n\n"
            f"**MODE B — ROUGH IDEA** (the brief is a short product "
            f"description, a marketing angle, or a one-line concept — NO "
            f"explicit dialogue):\n"
            f"  → Write a fresh creator-voice script in CLEAN URBAN INDIAN "
            f"ENGLISH by default (an English-medium-educated Mumbai/Delhi "
            f"creator who consumes American/British online content; the "
            f"Indian accent is in the voice/audio direction, NOT in the "
            f"vocabulary).\n"
            f"  → Do NOT inject Hindi words by default. \"yaar\", \"matlab\", "
            f"\"bhai\", \"bas\", \"na\" should NOT appear unless the brief "
            f"explicitly asks for Hinglish or already contains Hindi.\n"
            f"  → If (and only if) the brief explicitly says something like "
            f"\"Hinglish\", \"with Hindi sprinkled in\", \"desi creator voice\", "
            f"or itself contains Hindi words, THEN apply the Hinglish "
            f"lexical rules below — sparingly, 1 Hindi word per 8–12 "
            f"English words, only at emphasis points.\n\n"
            f"### TARGET\n"
            f"DURATION: {duration_s} seconds, split into {n_chunks} chunks "
            f"of {CHUNK_SECONDS}s each.\n"
            f"WORD DENSITY (Mode B only): ~2.8 words/sec for Indian English "
            f"UGC = ~{words_per_chunk} words per chunk, ~{total_words} "
            f"words total. Do NOT exceed in Mode B.\n\n"
            f"### HINGLISH RULES — APPLY ONLY IF THE BRIEF EXPLICITLY ASKS\n"
            f"Default is clean urban Indian English. Apply the rules below "
            f"ONLY if the brief uses words like \"Hinglish\", \"desi voice\", "
            f"\"with Hindi sprinkled in\", or itself contains Hindi.\n"
            f"- When applied: English is the SPINE of every sentence "
            f"(80-85%), Hindi appears as 2-4 word emphasis bursts ONLY at "
            f"natural emphasis points, not in every sentence.\n"
            f"- Approved Hindi emphasis words: yaar, matlab, bas, "
            f"na/hai na, bilkul, scene, bhai, accha, arre, pakka. CTA-"
            f"style: try karke dekho, link bio mein hai.\n"
            f"- Frequency target when applied: ~1 Hindi word per 8-12 "
            f"English words. Sparingly. Never one Hindi word per chunk.\n"
            f"- CASUAL HINGLISH PHONETICS & SPELLING (Urban Social Standard):\n"
            f"    - Use modern casual chat/social spelling that TTS engines pronounce naturally.\n"
            f"    - PREFER 'bohot' instead of formal 'bahut'.\n"
            f"    - PREFER 'accha' instead of 'acha'.\n"
            f"    - PREFER 'sahi', 'ekdam', 'mast', 'chahiye', 'waise' over formal/textbook Hindi spellings.\n"
            f"    - Avoid textbook/pure Hindi vocabulary (e.g., 'bahut', 'parantu', 'atyadhik', 'kintu'). Use real Gen-Z/Millennial chat Hindi ('bohot', 'lekin', 'par').\n"
            f"- FORBIDDEN under all circumstances:\n"
            f"    Full Hindi sentences: \"Maine isko iske andar daaldiya hai\" — ❌\n"
            f"    Hinglish run-ons: \"Yaar matlab literally kya scene hai\" — ❌\n"
            f"    Forcing yaar/matlab/bhai into every chunk for flavour — ❌\n\n"
            f"### STRUCTURE\n"
            f"Hook (chunk 0) → pivot (chunk 1) → product benefits (middle) "
            f"→ CTA (last chunk).\n"
            f"- Hook: pattern-interrupt opener. \"Okay so —\", \"Guys, \", "
            f"\"Wait wait wait\", \"POV: \", \"No but listen\".\n"
            f"- Body: concrete benefits, named numbers from the brief, 1-2 "
            f"sentences max per chunk.\n"
            f"- CTA: soft, Hinglish (\"try karke dekho\", \"link bio mein hai\").\n\n"
            f"### SILENT REACTION BEATS\n"
            f"Leave room for non-dialogue beats — a bite, a sip, a dab, a "
            f"thoughtful pause, a small head shake. Real UGC creators do "
            f"these every 1-2 seconds. Do NOT fill every second with "
            f"words. Some chunks may have just 5-10 words of dialogue + "
            f"a silent reaction beat — that's fine.\n\n"
            f"Set n_chunks = {n_chunks}. tone_notes should describe the "
            f"voice character (e.g., \"24-yo Mumbai creator, English-"
            f"medium, fast casual, slight uptick at phrase ends, mid-laugh "
            f"energy, not announcer\").\n\n"
            f"### BRIEF PARSING & NOISE HANDLING\n"
            f"- The input brief may contain raw notes, typos, shorthand (e.g., '30s', '15 sec'), metadata tags (e.g., '[Hook]', 'CTA:'), or informal bullet points.\n"
            f"- IGNORE metadata tags, section headers, and duration indicators (e.g., '30s', '30 second video') — DO NOT include these shorthand notes as spoken dialogue in the final script.\n"
            f"- Clean up obvious typos and spelling mistakes automatically while preserving product names, brand identity, and intended tone.\n"
            f"- When evaluating Mode A vs Mode B, ignore metadata markers like '30s' or '[Hook]' and evaluate whether actual spoken lines/dialogue were provided.\n\n"
            f"BRIEF:\n{brief}"
        )
        async def _do():
            return await self._generate(
                tier="fast", prompt=prompt, schema=ScriptDraft,
            )
        return await self._with_retry(_do, label="generate_script")

    async def write_screenplay(self, *, brief: str,
                               script: ScriptDraft) -> Screenplay:
        """Plan the visual screenplay BEFORE dialogue distribution.

        v2 plans visuals through the SCENE-FIRST director (plan_timeline); this
        is the v1 path, still called by the v1 UI tab — do not delete.

        Returns one SceneBeat per chunk. v2.4: strongly prefers STATIC
        camera moves (Seedance handles those far better than pan/tilt),
        bakes silent reaction beats into the visual_action, and uses
        CHUNK_SECONDS from config (currently 8s).
        """
        n = script.n_chunks
        total_s = n * CHUNK_SECONDS
        prompt = (
            f"You are a UGC ad director planning the visuals for a "
            f"{total_s}-second video. The video has {n} sequential "
            f"{CHUNK_SECONDS}-second chunks (each becomes one Seedance "
            f"generation).\n\n"
            f"Design exactly {n} scene beats, one per chunk. Each beat MUST "
            f"declare ONE framing AND ONE camera_intent — no overlays, no "
            f"compound moves.\n\n"
            f"### Framing palette\n"
            f"  - selfie: tight handheld phone-selfie, intimate confessional\n"
            f"  - tripod: static medium shot from a propped phone, hands free\n"
            f"  - counter: phone propped over the counter looking down\n"
            f"  - walking: handheld walking-and-talking (USE SPARINGLY — "
            f"walking is the hardest motion for Seedance to render cleanly; "
            f"only pick when the script clearly demands movement)\n\n"
            f"### Camera moves — STRONGLY PREFER 'static'\n"
            f"Seedance handles static shots far better than moving cameras. "
            f"At least 2 of every 3 chunks should be 'static'. Use 'slow "
            f"push-in' or 'slow pull-out' sparingly for emphasis at the "
            f"hook or CTA only. AVOID tilt-down/tilt-up/pan unless the "
            f"script explicitly demands following an action.\n"
            f"  - static (default, safest, use majority of chunks)\n"
            f"  - slow push-in (emphasis, max 1-2 chunks)\n"
            f"  - slow pull-out (reveal, sparingly)\n"
            f"  - tilt-down / tilt-up / pan-left / pan-right (only when "
            f"absolutely necessary)\n\n"
            f"### Visual action — keep it SIMPLE\n"
            f"Each visual_action describes ONE clear thing happening in "
            f"that {CHUNK_SECONDS}-second window. Examples that work:\n"
            f"  - 'Creator holds the bowl mid-laugh, delivers hook'\n"
            f"  - 'Creator takes a small bite, brief silent reaction beat, then speaks'\n"
            f"  - 'Creator gestures with one hand, product packet visible on counter'\n"
            f"  - 'Creator looks into camera, soft confident smile, CTA delivery'\n"
            f"AVOID: walking around, picking up + setting down + gesturing, "
            f"multi-step product demos, hand-position-changes mid-shot.\n\n"
            f"### Silent reaction beats — bake them in\n"
            f"Real Indian UGC creators pause for 0.5-1s after key statements "
            f"for a silent reaction beat (eyes widen, slight nod, head shake, "
            f"take a bite/sip/dab). At least one chunk should be DOMINATED "
            f"by a silent beat with minimal dialogue. Mark these explicitly "
            f"in the visual_action.\n\n"
            f"Write a one-paragraph overall_arc explaining chunk-to-chunk "
            f"visual flow.\n\n"
            f"BRIEF:\n{brief}\n\nDIALOGUE SCRIPT:\n{script.full_script}\n\n"
            f"Return JSON matching the Screenplay schema with exactly {n} "
            f"scene_beats indexed 0..{n - 1}."
        )
        async def _do():
            return await self._generate(
                tier="fast", prompt=prompt, schema=Screenplay,
            )
        return await self._with_retry(_do, label="write_screenplay")

    async def split_script(self, *, brief: str, script: ScriptDraft,
                           screenplay: Screenplay,
                           n_chunks: int) -> list[ChunkSpec]:
        """Carve dialogue into per-chunk ChunkSpecs aligned to the screenplay.

        v2 distributes dialogue through the SCENE-FIRST director (plan_timeline);
        this is the v1 path, still called by the v1 UI tab — do not delete.

        Each ChunkSpec's action and framing are copied from the corresponding
        SceneBeat — the screenplay does the visual thinking; this call does
        the dialogue distribution.
        """
        beats_json = json.dumps(
            [b.model_dump() for b in screenplay.scene_beats], indent=2,
        )
        words_per_chunk = int(CHUNK_SECONDS * 2.8)
        prompt = (
            f"Distribute the dialogue script across exactly {n_chunks} "
            f"{CHUNK_SECONDS}-second chunks. The visual screenplay (one beat "
            f"per chunk) is provided below — carve the dialogue so it lands "
            f"within each beat's visual moment.\n\n"
            f"### DIALOGUE PRESERVATION RULE (CRITICAL)\n"
            f"The `dialogue` field in each chunk MUST be a verbatim slice "
            f"of the DIALOGUE SCRIPT below. You are ONLY chopping the "
            f"script at sentence boundaries to fit chunk timings — NOT "
            f"rewriting, NOT paraphrasing, NOT adding Hindi words.\n"
            f"- If the script says \"Most people fail their health goals\", "
            f"your chunk says exactly that — NOT \"Guys, most people fail "
            f"their health goals, yaar.\"\n"
            f"- If the script says \"EXTREMES that our routines DONT LAST\", "
            f"preserve the capitalisation and typo verbatim.\n"
            f"- If the script has scene directions in parens like \"(start "
            f"eating from the bowl)\", route that into `action` for the "
            f"correct chunk, NOT into `dialogue`.\n"
            f"- DO NOT add Hindi words (yaar, matlab, bas, bhai, na) "
            f"unless they ALREADY appear in the source script.\n"
            f"- The full set of chunk.dialogue strings concatenated should "
            f"reconstruct the DIALOGUE SCRIPT below (minus parenthetical "
            f"directions which move to action).\n\n"
            f"For each chunk return:\n"
            f"  - index (0..{n_chunks - 1}), t_start, t_end\n"
            f"  - dialogue: VERBATIM slice of the script for this "
            f"{CHUNK_SECONDS}s window. May be empty/very short if the beat "
            f"is a silent reaction (e.g., a chunk dedicated to the '(start "
            f"eating from the bowl)' direction).\n"
            f"  - action: what's visible at the OPENING of this chunk "
            f"(t_start). This is the chunk's FIRST FRAME — describe the "
            f"static composition: where the creator is, hand position, "
            f"facial expression, product visibility, background state. "
            f"DON'T describe motion here; describe the still moment.\n"
            f"  - closing_action: what's visible at the CLOSING of this "
            f"chunk (t_end). This is the chunk's LAST FRAME — same "
            f"description style as action, but for the END state. CRITICAL: "
            f"for chunks i<{n_chunks - 1}, closing_action MUST match the "
            f"NEXT chunk's action — they describe the SAME boundary frame "
            f"shared between two chunks. For the last chunk, closing_action "
            f"is the final freeze-frame of the whole video.\n"
            f"  - framing: copy the framing from the corresponding scene "
            f"beat — do not change it\n"
            f"  - summary: one short line of what just happened, so the "
            f"next chunk's Seedance prompt can read it for continuity\n\n"
            f"WORD DENSITY (CRITICAL): ~2.8 words/sec for Indian English "
            f"UGC = ~{words_per_chunk} words per chunk MAX. Slower than US "
            f"UGC. Leave room for breath and silent beats. Going over "
            f"breaks the lipsync.\n\n"
            f"Tone: {script.tone_notes}\n\n"
            f"BRIEF:\n{brief}\n\n"
            f"DIALOGUE SCRIPT:\n{script.full_script}\n\n"
            f"SCENE BEATS:\n{beats_json}\n\n"
            f"Return a JSON array of ChunkSpec objects, ordered by index."
        )
        async def _do():
            return await self._generate(
                tier="fast", prompt=prompt, schema=list[ChunkSpec],
            )
        return await self._with_retry(_do, label="split_script")

    async def describe_product(self, *, product_ref_url: str) -> str:
        """ONE factual sentence about WHAT the product physically is, read off
        its photo. The planner stages every product shot BLIND (it never sees
        the image, only `product_present: bool`) and so hallucinated container
        types/colours from the brief's wording — the "BOX described as a
        bottle" class of bug. This grounds its broll_shot/visual_note text in
        the real item (2026-07-31). Low temperature: fidelity, not creativity."""
        prompt = (
            "Describe this product in ONE factual sentence (max 40 words) for "
            "a director who cannot see the photo: the container/item type and "
            "form factor (bottle/jar/box/pouch/sachet/tube/device/garment...), "
            "its primary colours and material, and where the label/branding "
            "sits. Physical facts only — no marketing language, no invented "
            "details, do not read out the label text."
        )
        async def _do():
            return await self._generate(
                tier="fast", prompt=prompt, image_url=product_ref_url,
                temperature=0.1,
            )
        return await self._with_retry(_do, label="describe_product")

    async def extract_brief_directives(self, *, brief: str) -> BriefDirectives:
        """Extract the brief's EXPLICIT instructions before planning.

        Fail-open at the call site: the pipeline proceeds without directives
        if this raises. Low temperature — extraction is fidelity work, the
        opposite of creative work."""
        prompt = (
            "Extract the EXPLICIT instructions from this UGC-ad brief. You "
            "are a faithful extractor, not a creative: copy, never invent.\n\n"
            "1. explicit_script — ONLY if the brief contains actual spoken "
            "creator dialogue (a labeled script block, or lines clearly "
            "written to be said to camera). Copy the SPOKEN WORDS "
            "CHARACTER-FOR-CHARACTER (words, capitals, the spoken text's own "
            "punctuation, line breaks, parenthetical stage directions) — but "
            "DROP production markup that is never spoken: section labels "
            "like 'HOOK VO :', 'SCENE 2:', 'CTA -', speaker names, and the "
            "quotation marks wrapping scripted lines. A product description, "
            "tone words, or bullet points of ideas are NOT a script → null.\n"
            "2. visual_directives — each explicit instruction about looks/"
            "shots/framing/screenplay/setting/props/wardrobe/vibe/brand "
            "visibility, one per item, keeping the brief's wording.\n"
            "3. character_directives — each explicit instruction about the "
            "creator/model themselves.\n\n"
            f"BRIEF:\n{brief}"
        )
        async def _do():
            return await self._generate(
                tier="fast", prompt=prompt, schema=BriefDirectives,
                temperature=0.2,
            )
        return await self._with_retry(_do, label="extract_brief_directives")

    async def plan_timeline(
        self, *, brief: str, character: "CharacterDescription",
        product_present: bool, target_duration_s: int | None = None,
        hook_type: str | None = None,
        narrative_structure: str | None = None,
        words_per_second: float = 2.0,
        directives: "BriefDirectives | None" = None,
        product_description: str = "",
    ) -> VideoTimeline:
        """Plan a two-track UGC ad timeline as a SCENE-FIRST director.

        Detects whether the brief contains an explicit script: MODE A copies it
        VERBATIM, MODE B writes a fresh creator-voice script. Then it DESIGNS A
        SHOT LIST by scene logic — deciding how many shots, each shot's purpose
        (ad_section), shot_type/framing/broll_type, a staged physically-plausible
        visual_note written KNOWING the creator's look + setting (the
        ``character`` arg), which contiguous full_script span each shot speaks,
        whether the creator is on screen (shows_creator), and beat_kind. It may
        insert "reaction" hold beats (empty dialogue + hold_s) for sensory/pause
        moments. Beats follow the ad anatomy (hook→problem→solution→proof→cta).

        character: the canonical creator (look / setting / energy), derived from
        the brief by ``describe_character`` BEFORE this call, so visual_notes can
        be staged for the actual person and scene.

        product_present: whether a product reference image exists — gates the
        product-fidelity guidance.

        target_duration_s: optional. When None (the default — the UI no longer
        forces a duration), the AI decides: a verbatim script runs as long as
        it reads, and a fresh script is sized to a self-chosen 15-45s. When the
        caller DOES pass a target, it's enforced as a word budget (Mode B only).

        hook_type / narrative_structure: optional overrides. When provided the
        planner MUST use them; when None the planner chooses from the brief.

        words_per_second: words-per-second used for the budget / guidance.
        Default 2.0 — measured eleven_v3 expressive pacing across real runs is
        1.78-2.15 w/s (tags + …/— pauses eat time). The pipeline's
        duration-correction loop passes a per-script MEASURED value here when
        re-planning an overshot, explicitly-targeted Mode-B VO.
        """
        ai_decides = target_duration_s is None
        words_target = (None if ai_decides
                        else int(target_duration_s * words_per_second))
        # Validator total-word cap (+10%) — only when a target was forced.
        max_words = None if words_target is None else int(words_target * 1.10)
        override_block = ""
        if hook_type:
            override_block += f"- You MUST use hook_type = '{hook_type}'.\n"
        if narrative_structure:
            override_block += (
                f"- You MUST use narrative_structure = "
                f"'{narrative_structure}'.\n"
            )
        if override_block:
            override_block = "### FORCED CHOICES\n" + override_block + "\n"

        # Extracted directives render as an explicit checklist right under the
        # brief. The brief itself now sits AT THE TOP of the prompt — it used
        # to land ~210 lines below the "obey the brief" mandate, after every
        # rule section, where its instructions routinely lost to the generic
        # style rules (2026-07-31).
        directives_block = ""
        if directives and (directives.visual_directives
                           or directives.character_directives):
            items = [f"  - {d}" for d in (directives.visual_directives
                                          + directives.character_directives)]
            directives_block = (
                "EXTRACTED DIRECTIVES (satisfy EVERY one in the shot list — "
                "each is a hard requirement, not inspiration):\n"
                + "\n".join(items) + "\n"
            )

        # The director already knows the creator (describe_character ran first),
        # so every visual_note is staged for THIS person in THIS setting.
        character_block = (
            "### THE CREATOR (already cast — stage every shot for THIS person)\n"
            f"  - {character.age}yo {character.gender}, {character.ethnicity} "
            f"({character.region}), {character.skin_tone} skin\n"
            f"  - hair: {character.hair}\n"
            f"  - face: {character.face}\n"
            f"  - wardrobe: {character.wardrobe}\n"
            f"  - setting: {character.setting}\n"
            f"  - energy: {character.energy}\n"
            "Write each visual_note KNOWING this look and setting — a concrete, "
            "physically-plausible staged moment for HER, not a generic stock "
            "direction. NEVER stage the recording device in a visual_note: no "
            "phone, tripod, gimbal, ring light or selfie stick in frame — the "
            "camera is the unseen POV (the only exception is a 'mirror' framing, "
            "where the phone shows in the reflection).\n"
            "GESTURE SAFETY: never stage a finger pointed AT the camera/lens — "
            "foreshortened fingers warp in generation; use an open-palm gesture, "
            "a nod, or a point angled off-axis instead. And never stage a hand "
            "stroking/smoothing fabric or objects for the whole shot — stage ONE "
            "brief touch or press that settles, then the hand rests.\n\n"
        )

        duration_line = (
            f"a complete {target_duration_s}-second"
            if not ai_decides else "a complete"
        )
        mode_b_budget = (
            "DURATION: choose target_duration_s yourself, between 15 and 45 "
            "seconds, based on how much there is to say (teaser / simple = "
            "15-25s; more to explain = 30-45s). Size the script to "
            f"~{words_per_second:.1f} words/sec at that duration and set "
            "target_duration_s to your chosen value."
            if ai_decides else
            f"HARD WORD BUDGET: at most {words_target} spoken words for "
            f"{target_duration_s}s (~{words_per_second:.1f} w/s — expressive "
            "TTS with pauses reads SLOWER than you think; over-budget scripts "
            "overshoot the video and get REJECTED). Set target_duration_s = "
            f"{target_duration_s}."
        )
        # Sanitize BEFORE seeding: the planner must see (and copy) only the
        # SPOKEN words — handing it "HOOK VO :" labels teaches it to echo
        # them into full_script, which then fails the beat-partition check.
        forced_script = _strip_production_markup(
            directives.explicit_script or "").strip() if directives else ""
        mode_detection = (
            # The extractor already settled the mode — do not let the planner
            # re-classify (its self-classification is how user scripts got
            # silently paraphrased; the validator now enforces equality).
            "### SCRIPT — MODE A IS FORCED (the brief contains this script)\n"
            "The client wrote the script below. Set script_source='verbatim' "
            "and use MODE A — this is not your call to make.\n"
            f"CLIENT SCRIPT (copy word-for-word):\n{forced_script}\n\n"
            if forced_script else
            "### SCRIPT — MODE DETECTION (read the brief carefully FIRST)\n"
            "The brief EITHER contains an explicit spoken script (actual "
            "creator dialogue — talking-to-camera copy, a labeled 'Script:' "
            "block, or lines clearly written to be spoken, with or without "
            "scene directions in parens like '(wink)'), OR it is just a "
            "rough idea / product description. Decide which, and set "
            "script_source accordingly.\n\n"
        )
        script_section = (
            mode_detection
            + "**MODE A — brief CONTAINS an explicit script → "
            "script_source='verbatim':**\n"
            "  - full_script = that script, COPIED WORD-FOR-WORD. Preserve "
            "every word, exclamation mark, capital, em-dash, ellipsis, comma "
            "and line break EXACTLY. This is the creative team's final copy — "
            "faithful reproduction is the #1 priority. DO NOT paraphrase, "
            "summarise, reorder, shorten, 'tighten', re-punctuate, or inject "
            "Hindi / filler words.\n"
            "  - Parenthetical SCENE DIRECTIONS like '(wink)' / '(takes a "
            "bite)' are NOT spoken — REMOVE them from full_script and express "
            "them in the matching beat's visual_note instead.\n"
            "  - The word budget DOES NOT APPLY in Mode A — keep the script's "
            "length; estimate target_duration_s from its spoken length.\n\n"
            "**MODE B — rough idea, NO explicit dialogue → "
            "script_source='generated':**\n"
            "  - Write a fresh full_script in clean casual urban Indian English "
            f"(English-medium Mumbai/Delhi creator). {mode_b_budget} Do NOT pad "
            "or stretch; if the idea is short, fill runtime with B-roll, NOT "
            "slower talking. Only use Hindi if the brief asks.\n"
            "  - Write like ONE real person talking to camera — texting their "
            "best friend, NEVER a brand or an announcer. If a line sounds like "
            "an ad read, rewrite it.\n"
            "    - Contractions EVERYWHERE (it's, you're, I've, don't). Mostly "
            "short punchy fragments; vary sentence length. Use 'you' and 'I'.\n"
            "    - 1-3 natural discourse markers across the WHOLE script (okay "
            "so / honestly / no joke / wait / literally) — at most one per "
            "section.\n"
            "    - At least ONE hyper-specific number or timeframe — WRITE IT "
            "IN THE WORDS YOU WANT SPOKEN (e.g. 'four ninety-nine', 'fifty "
            "percent off', 'in three weeks'), never a bare symbol/digit string "
            "like '₹499' or '50%' (the voice mispronounces those).\n"
            "    - BANNED: parallel triplets, buzzwords (game-changer, "
            "revolutionary, unlock, elevate), clean press-release endings.\n"
            "    - Pick ONE core emotion (relief / excitement / transformation "
            "/ curiosity) and let word choice carry it.\n\n"
        )
        product_fidelity = (
            (
                f"### PRODUCT FIDELITY + GROUNDED PRODUCT SHOTS (read carefully)\n"
                + (
                    f"THE ACTUAL PRODUCT (read off its photo — this is what "
                    f"the item physically IS): {product_description}\n"
                    f"Write EVERY product visual_note/broll_shot for THIS "
                    f"exact item — its real container type and colours — "
                    f"never a different form factor the brief's wording might "
                    f"suggest.\n"
                    if product_description else
                    f"Infer WHAT this product is from the brief (a fragrance, "
                    f"a serum, a snack).\n"
                )
                + f"Pick a moment that sells it the way a REAL "
                f"person would show it in THIS exact setting — casual and "
                f"believable, NEVER a staged studio/stock 'hero' composition.\n"
                f"GROUNDED — every product `visual_note` MUST:\n"
                f"  - Keep the product IN the real scene (same surface, room, "
                f"car, light as the surrounding shots). Do NOT lift it into open "
                f"sky, a seamless backdrop, or abstract negative space.\n"
                f"  - Put it somewhere physically real: held naturally in one "
                f"hand at relaxed chest height, or resting on an actual surface "
                f"that exists in the scene (table, lap, counter, dashboard). "
                f"NEVER 'held up' against the sky or a window, balanced on an "
                f"edge / ledge / window frame, or floating in the frame.\n"
                f"  - Keep the surrounding environment legible in frame — don't "
                f"crop the world away to isolate the product.\n"
                f"FIDELITY — the EXACT container is fixed by the reference image, "
                f"so in every `visual_note`/`broll_shot`:\n"
                f"  - The shape, colour, label artwork, branding and VARIANT come "
                f"ENTIRELY from the reference image — do NOT invent a different "
                f"container type, colour, flavor, variant, or a multi-pack, and "
                f"do NOT describe the label text/artwork (the model copies it). "
                f"The product keeps its EXACT shape and label — no morphing.\n"
                f"  - Only state prep methods (hot water, spray, etc.) the BRIEF "
                f"supports.\n"
                f"HAND DISCIPLINE (so a creative shot doesn't break — the #1 "
                f"failure is hands): a product shot is ONE clean deliberate "
                f"action (or a steady hold) by ONE pair of hands. NEVER stage a "
                f"second hand joining or 'caressing' the product, a hand entering "
                f"from an odd angle, stroking, or constant re-gripping. Pair a "
                f"moving product with a LOCKED camera (never move both). Any hand "
                f"holding the product is the CREATOR'S OWN hand (it matches her — "
                f"a woman's hand, her skin tone, slim natural fingers), NEVER a "
                f"stranger's or a man's hand.\n"
                f"PHYSICAL LOGIC: the product is sealed exactly as its reference "
                f"shows it (cap/lid ON). Do NOT stage it spraying, spritzing, or "
                f"the nozzle being pressed unless the shot first clearly shows the "
                f"cap REMOVED and set aside — a closed bottle cannot spray. When "
                f"in doubt, keep the cap on and use a hold / slow turn / label "
                f"reveal.\n\n"
            )
            if product_present else
            (
                "### NO PRODUCT REFERENCE\n"
                "There is NO product reference image for this ad. Do NOT invent "
                "a packaged product or show one on screen; keep the visuals on "
                "the creator, her hands, and the setting.\n\n"
            )
        )
        prompt = (
            f"You are a UGC ad director. Plan {duration_line} vertical (9:16) "
            f"Indian creator ad as a two-track timeline. Work in TWO steps: "
            f"FIRST write (or copy) the script, THEN design a SHOT LIST by "
            f"scene logic — for each shot decide its purpose (ad_section), the "
            f"shot_type/framing/broll_type, a staged physically-plausible "
            f"visual_note for THIS creator, which contiguous slice of "
            f"full_script it speaks, whether the creator is on screen "
            f"(shows_creator), and whether it is a speech shot or a silent "
            f"reaction hold (beat_kind). Let the SCENE decide how many shots "
            f"there are — not a fixed words-per-beat quota.\n\n"
            f"### TRACK MODEL (critical)\n"
            f"ONE continuous voiceover (the creator talking). The VISUAL "
            f"track cuts between:\n"
            f"  - talking_head: the creator's face on camera speaking "
            f"(lip-synced). Set `framing`.\n"
            f"  - broll: a cutaway (product/hands/scene) while the SAME "
            f"voiceover keeps playing under it (no face). Set `broll_type` "
            f"(+ optional `broll_shot` specifics).\n"
            f"Concatenating the dialogue_text of every SPEECH beat in order "
            f"MUST reconstruct full_script EXACTLY (same characters/spaces/"
            f"punctuation). Reaction (silent-hold) beats carry an EMPTY "
            f"dialogue_text and are excluded from that reconstruction.\n\n"
            "### VOICE MODE (native audio — set voice_mode on EVERY beat)\n"
            "The voice is generated by the video model from dialogue_text. "
            "Keep the edit FAST and CUT-TO-CUT, and keep the voice FLOWING:\n"
            "  - on_camera: talking_head speaking to the lens.\n"
            "  - background_vo: a broll/product/action cutaway where the SAME "
            "voice KEEPS TALKING off-screen. This is the DEFAULT for broll — "
            "in most cases the dialogue continues under the broll.\n"
            "  - silent: ONLY when the shot has no line at all (a pure action, "
            "holding the product, or a reaction hold). Use sparingly.\n"
            "Do NOT split a sentence across two beats — each beat's "
            "dialogue_text must be a whole phrase/sentence so the voice never "
            "repeats or overlaps at a cut.\n\n"
            f"### THE BRIEF (the client's spec — everything below serves THIS)\n"
            f"{brief}\n\n"
            f"{directives_block}"
            f"### BRIEF DIRECTIVES ARE MANDATORY\n"
            f"The brief is the client's spec, not inspiration. Anything it states "
            f"explicitly — a required VIBE/tone, specific shots or moments, props, "
            f"wardrobe, framing/camera rules, the setting, product-shot rules, the "
            f"brand name being visible — is a HARD REQUIREMENT you MUST satisfy in "
            f"the shot list. Do not drop, soften, or generically paraphrase a stated "
            f"directive.\n"
            f"  - VIBE: if the brief names a vibe (e.g. 'sensuous + boss lady'), "
            f"every visual_note and the script's word choice must EMBODY it — it is "
            f"the emotional through-line, not a label.\n"
            f"  - IMPORTANT / 'this shot is important' moments: when the brief "
            f"describes a specific shot or moment (even loosely, even 'use it where "
            f"you see fit'), you MUST dedicate a REAL, on-screen beat to it that the "
            f"viewer actually sees for a few seconds — almost always a B-ROLL "
            f"cutaway (broll, shows_creator as appropriate) running UNDER the "
            f"voiceover, staged FAITHFULLY and VIVIDLY to the brief's exact "
            f"description, and placed at/near any dialogue line the brief suggests. "
            f"NEVER shrink a described important shot into a sub-second silent "
            f"reaction hold, and never omit it. If the brief says a moment is "
            f"important, it earns its own visible beat.\n\n"
            f"{character_block}"
            f"{script_section}"
            f"### SPOKEN RHYTHM (the script IS the prosody score)\n"
            f"full_script is read by eleven_v3 TTS, which obeys punctuation as "
            f"prosody and inflects MORE at low stability. In MODE A (verbatim) "
            f"PRESERVE the script's existing punctuation exactly — do NOT add "
            f"or remove … / — / CAPS. In MODE B, bake the delivery into the "
            f"text:\n"
            f"  - Use … (ellipsis) for a thinking/emotional pause or trail-off "
            f"('I mean… it actually worked').\n"
            f"  - Use — (spaced em-dash) for a hard cut or self-correction "
            f"('I'm not kidding — like, life-changing').\n"
            f"  - ONE ALL-CAPS word per sentence MAX for emphasis ('this "
            f"ACTUALLY works'). \n"
            f"  - Use … and — SPARINGLY: at most one or two per section.\n"
            f"  - Do NOT emit SSML or <break> tags — eleven_v3 reads them "
            f"literally.\n"
            f"  - AUDIO TAGS ARE THE MODULATION. eleven_v3 gets its emotional "
            f"range from inline tags, NOT from settings — an untagged script "
            f"reads MONOTONE no matter how the voice is configured. Direct the "
            f"performance the way you would direct an actor.\n"
            f"    DELIVERY tags change only HOW the next words are said and add "
            f"no audio, so use them at EVERY genuine delivery shift — aim for "
            f"roughly ONE per sentence, and give each one a WHOLE sentence to "
            f"land (a tag on three words cannot develop). Vocabulary — use ONLY "
            f"these: {', '.join(_DELIVERY_TAGS)}.\n"
            f"    REACTION tags emit an actual SOUND: {', '.join(_REACTION_TAGS)}. "
            f"These are limited to {_MAX_REACTION_TAGS} in the WHOLE script and "
            f"only for a GENUINE reaction — their audio has no characters to "
            f"align to and must be absorbed by the following beat.\n"
            f"    Let the emotion ARC across the ad: the hook and problem should "
            f"not be delivered in the same register as the solution or CTA.\n"
            f"    Rules: place a tag immediately BEFORE the words it modifies; "
            f"ONE primary emotion per sentence; never stack incompatible tags; "
            f"invent NO tags outside the two lists above (an unknown tag gets "
            f"SPOKEN ALOUD). Each tag must sit ENTIRELY inside ONE beat's "
            f"dialogue_text — never split a tag across beats. A tag may only "
            f"OPEN a line, NEVER close it: dialogue_text that ENDS with a tag "
            f"gets REJECTED (a trailing tag's audio leaks into the next "
            f"beat's slice).\n"
            f"  - PRONUNCIATION: after writing full_script, scan it for words a "
            f"TTS trained mainly on Western English would MISPRONOUNCE — "
            f"Sanskrit/Ayurvedic and Hindi terms, brand and product names, "
            f"coined words — and list each in `pronunciations` with a plain "
            f"respelling (hyphenated syllables, STRESSED syllable in CAPS: "
            f"'Ayurvedic' -> 'aa-yur-VAY-dik'). Leave full_script itself spelled "
            f"NORMALLY — the respelling is applied only to the audio, so "
            f"captions stay correct. Do NOT list ordinary English words: every "
            f"entry changes the spoken word, so a wrong entry makes it worse.\n"
            f"  - VOICE DIRECTION: set `voice_direction` on every SPEAKING beat "
            f"— you are directing the read, not just writing words. Shape: 'In "
            f"a (emotional state) state, with a (tone) tone and a (pace) pace', "
            f"under 14 words. Make it TRACK the arc: the hook, the problem, the "
            f"proof and the CTA must not share one register. The pace must "
            f"always be brisk/real-time (fast, brisk, lively, steady) — never "
            f"slow or unhurried. Empty for reaction/silent beats.\n\n"
            f"### AD ANATOMY (enforce strictly — tag every beat's ad_section)\n"
            f"Each section has its OWN emotional word-choice — do not let it "
            f"flatten into one tone:\n"
            f"  - hook (0-3s): ONE pattern-interrupt line. Pick a hook_type "
            f"(pov/stop_scrolling/controversial/curiosity_gap/question/"
            f"listicle/result_first/callout/myth_buster). ANY high-energy "
            f"pattern-interrupt frame works — approach, found_it, punch_in, "
            f"high_angle_drop, pov_walk, lav_mic, follow_turn, a punchy "
            f"product B-roll... pick what fits THIS brief, not a default. "
            f"Voice = bold, doubt-to-belief. Lands in <3s, NO brand name.\n"
            f"  - problem (3-10s): name the pain / context the viewer "
            f"recognises. Voice = pain words ('tried everything', 'nothing "
            f"worked', 'kept getting worse').\n"
            f"  - solution (10-20s): the product / demo — LEAN on B-roll "
            f"(hands_using, demo_steps, product_closeup, texture_pour). Voice "
            f"= DISCOVERY, first person ('then I found', 'finally', 'literally "
            f"changed'). The product is DISCOVERED, NEVER sold — never 'this "
            f"product offers / is designed to'.\n"
            f"  - proof: before_after or testimonial (may fold into "
            f"solution). Voice = casual specificity (a number, a timeframe, "
            f"'see?').\n"
            f"  - cta (last 3-5s): EXACTLY ONE call to action. Voice = warm, "
            f"direct, low-pressure ('just try it', 'you'll see').\n"
            f"Beats MUST be ordered hook → problem → (solution/proof) → cta: "
            f"hook first, then problem, then the body where solution and proof "
            f"beats may INTERLEAVE freely (feature → proof → feature → proof), "
            f"then cta last. A 'problem' beat may NOT follow a solution/proof "
            f"beat. Choose narrative_structure (pas/storytime/"
            f"day_in_life/testimonial/comparison/listicle/demo).\n\n"
            f"### PRODUCT NAME (top-level field)\n"
            f"Set product_name to the product's short spoken display name "
            f"(null if the brief has no product).\n\n"
            f"### BEAT RULES\n"
            f"- Let the SCENE drive the number of shots — cut when the visual "
            f"should change (a new action, a reveal, a reaction), NOT to hit a "
            f"word quota. A visual reset every ~4s roughly doubles retention vs "
            f"a static talking head, so don't park on one talking_head for "
            f"long; resolve a long passage with a B-roll cutaway under the same "
            f"VO.\n"
            f"- GUARDRAILS (enforced): ≤{_MAX_BEAT_WORDS} spoken words per "
            f"beat (≈2-3s) — a beat over {_MAX_BEAT_WORDS} words gets REJECTED "
            f"*if it can be split* at an interior phrase boundary, so split it "
            f"into a talking_head + an immediate B-roll cutaway. AT MOST ONE "
            f"beat in the whole video may run {_MAX_BEAT_WORDS + 1}-"
            f"{_MAX_HELD_BEAT_WORDS} words (≈4-5s) — the one earned hold for a "
            f"key line or product hero; a second one gets REJECTED. EXCEPTION: "
            f"if a single sentence genuinely can't be cut at a phrase boundary "
            f"(a long verbatim line), keep it as ONE beat and cover it with a "
            f"B-roll cutaway under the VO — never force a mid-sentence cut. "
            f"Count words, not seconds (you cannot predict seconds; you can "
            f"count words).\n"
            f"- HARD MIN 5 spoken words for a talking_head beat — Seedance's "
            f"lip-sync reference audio needs ≥2s of speech, so a 1-4 word "
            f"talking_head gets REJECTED. Give a shorter line to a broll beat "
            f"(the VO carries it) or merge it into the adjacent beat.\n"
            f"- Cut beats ONLY at natural phrase boundaries — each beat's "
            f"dialogue_text must END at a sentence end (. ? ! …), a colon or "
            f"semicolon, an em-dash, "
            f"or a line break. NEVER split a single sentence across two beats "
            f"(a beat ending mid-clause gets REJECTED). DEFAULT to FAST cuts "
            f"(2-3s of speech) — a short COMPLETE sentence is a perfect "
            f"fast beat ('Skip the deodorant.' is a valid 2s beat). The ONE "
            f"held beat (see guardrails) is the only shot allowed to run "
            f"~4-5s — everything else stays fast, and lean on "
            f"B-roll cutaways to keep energy. If a sentence is long, a broll "
            f"cutaway can cover part of it, but the spoken text still belongs "
            f"to ONE beat boundary at the phrase edge.\n"
            f"- B-roll mix from the brief: product-demo brief = B-roll-heavy; "
            f"story/testimonial = more talking_head.\n"
            f"- talking_head beat → set framing + broll_type=null + "
            f"broll_shot=null. broll beat → set broll_type (+broll_shot) + "
            f"framing=null.\n"
            f"- target_duration_s: set it per the SCRIPT section above.\n\n"
            f"### SHOT DESIGN (set on every beat)\n"
            f"- shows_creator: True when the creator's face OR body OR hands "
            f"are on screen (talking_head, or a broll that shows her / her "
            f"hands). False for a pure product macro or a scene with no person "
            f"in it. Be honest — this decides whether her reference is used.\n"
            f"- beat_kind: 'speech' (she's saying the dialogue_text) or "
            f"'reaction' (a SILENT screen-time hold, NO speech). A 'reaction' "
            f"beat is ONLY for a brief, tiny dead-air pause (≤1s) where a real "
            f"creator would naturally stop talking — a quick breath, a beat to "
            f"let a line land. It MUST have an EMPTY dialogue_text and set hold_s "
            f"(e.g. 0.8). Use these RARELY (0-1 in the whole ad). A reaction beat "
            f"is NOT how you realize a meaningful or brief-mandated shot — a real "
            f"moment that needs to be SEEN (e.g. soaking in a lingering scent) is "
            f"a B-ROLL beat running under the VO, not a silent hold.\n"
            f"- FRAMING VARIETY (enforced): never the same framing on two "
            f"CONSECUTIVE talking_head beats (a repeat gets auto-swapped); "
            f"spread ≥4 distinct framings across the video, intercutting "
            f"still frames (floor_shot, low_angle_table, closeup_chest, "
            f"front_pano, top_angle, wide_side_low...) with moving ones "
            f"(vlog, side_track, pov_walk, walking...). The 2-phase frames "
            f"(high_angle_drop, follow_turn) are strong but hard for the "
            f"video model — use each AT MOST once per video.\n"
            f"- SCRIPT↔VISUAL LOCK: every beat's visual must SERVE its exact "
            f"dialogue line — a broll beat's broll_shot/visual_note depicts "
            f"the CLAIM of the words spoken over it (line about smell → she "
            f"reacts to the scent; line about texture → the texture; line "
            f"about mornings → the morning moment). Never a generic filler "
            f"scene that could sit under any line.\n"
            f"- ONE motion verb + ONE camera intent per shot. Never stack "
            f"subject motion AND camera motion in the same beat (a moving "
            f"creator under a moving camera is the #1 distortion source).\n"
            f"- FINE-MOTOR actions (tying laces, knotting, threading, "
            f"braiding, origami): KEEP the moment — but stage it as a short "
            f"CUT-ON-ACTION insert, the way a real editor shoots it. The "
            f"opening frame catches the hands ALREADY mid-action on the "
            f"object; ONE small decisive movement plays; the cut lands "
            f"before the intricate coordination completes. Never ask for "
            f"the full multi-step finger work to play out on camera (the "
            f"video model warps fingers/objects on it) — a 2s mid-action "
            f"insert reads as the whole action.\n"
            f"- Product / hands shots get exactly ONE quick, smooth push-in "
            f"(scale only — never orbit/pan/parallax over a product) with a "
            f"fast decisive subject action; never a slow creep or a frozen "
            f"hold.\n"
            f"- On a talking_head the FACE is never obscured — no hat brim or "
            f"sunglasses over the eyes; eyewear rides pushed up on the head. "
            f"The full face must be visible for lipsync.\n"
            f"- The product is held by EXACTLY ONE pair of hands belonging to "
            f"ONE person — never 'caressing', never extra / phantom hands, grip "
            f"stays constant.\n\n"
            f"{product_fidelity}"
            f"### HAND-BUDGET / PHYSICAL PLAUSIBILITY (CRITICAL)\n"
            f"The creator has TWO hands. Each framing consumes hand slots:\n"
            f"  - selfie         → 1 hand on phone. 1 hand free.\n"
            f"  - lav_mic        → 1 hand on lav. 1 hand free (or both free if "
            f"clipped on).\n"
            f"  - tripod / counter / approach / walking / punch_in → both "
            f"hands free.\n"
            f"  - product_reveal_pivot → both hands free (creator turns/"
            f"reaches; no phone in hand).\n"
            f"Hand-occupying props each take 1 slot: bowl=1, spoon=1, "
            f"product_pack=1, glass=1, lav_mic=1.\n"
            f"Rule: total prop slots used in a beat MUST be ≤ free hands.\n"
            f"  - selfie talking_head: max ONE prop (e.g. just the pack, OR "
            f"just a spoon — never bowl + spoon together; never bowl + pack).\n"
            f"  - If you want a creator EATING from a bowl with a spoon "
            f"(2 hand slots), the framing MUST be tripod / counter / "
            f"product_reveal_pivot — NEVER selfie.\n"
            f"  - If you want a creator HOLDING THE PACK while talking, the "
            f"framing MUST be tripod / counter / product_reveal_pivot — NEVER "
            f"selfie (their other hand is on the phone).\n"
            f"  - For 'eating shots' / 'spoonful' beats, prefer a B-roll "
            f"(broll_type=hands_using or product_in_scene) instead of a "
            f"talking_head — VO plays over silent hands.\n\n"
            f"### MOUTH-OCCUPYING ACTIONS (CRITICAL for lipsync)\n"
            f"talking_head beats are lip-synced, which needs a clean, frontal, "
            f"UNOBSTRUCTED, free mouth. A mouth that is eating/chewing/sipping "
            f"while 'talking' produces warped or double mouths — it is the #1 "
            f"lipsync-breaker.\n"
            f"  - NEVER assign these to a talking_head beat, ANY framing: "
            f"eating, biting, chewing, sipping, drinking, swallowing, tasting, "
            f"licking, applying anything to lips/face/mouth, spraying into the "
            f"mouth, brushing teeth, or any hand/object near or over the "
            f"mouth.\n"
            f"  - ROUTE every such action to a broll beat (broll_type="
            f"hands_using / demo_steps / texture_pour / product_in_scene): the "
            f"VO describes it over SILENT demo footage.\n"
            f"  - ALLOWED on talking_head: talking, gesturing, holding the "
            f"product UP (not using it), product_reveal_pivot. grwm is "
            f"talking_head ONLY for hair/cheek/eye steps — lip/lipstick "
            f"application must be broll.\n\n"
            f"{override_block}"
            f"Before answering, RE-READ THE BRIEF at the top — every explicit "
            f"directive in it must be visible in your shot list.\n\n"
            f"Return JSON matching the VideoTimeline schema."
        )
        async def _do(last_error: "Exception | None" = None):
            # Script-writing is the creative core — use the pro tier (was
            # flash@0.4, which left Pro idle on the live path) at a higher
            # temperature so the copy reads like a real person, not an ad.
            # Structural drift is caught by _validate_timeline → _with_retry.
            attempt_prompt = prompt
            if last_error is not None:
                # A prior attempt was REJECTED. Tell the model exactly what to
                # fix instead of blindly re-rolling — a blind re-roll of a
                # structural rejection can collapse to an empty timeline.
                attempt_prompt += (
                    "\n\n### CORRECTION REQUIRED — your previous attempt was "
                    "REJECTED. Fix what is listed below and change nothing "
                    "else:\n"
                    f"{last_error}\n"
                    "Re-emit the COMPLETE timeline — ALL beats partitioning the "
                    "whole script, in order. NEVER return an empty beats array."
                )
            # temperature reaches ONLY the Gemini fallback provider —
            # ClaudeBackend (the default) DROPS it on the pro tier (adaptive
            # thinking). Do not expect sampling to buy screenplay variety on
            # the live path: that is enforced by the FRAMING VARIETY rules +
            # the validator's framing repair.
            tl = await self._generate(
                tier="pro", prompt=attempt_prompt, schema=VideoTimeline,
                temperature=0.7,
            )
            _validate_timeline(                 # raises → retry on drift
                tl, max_words=max_words,
                expected_script=forced_script or None,
            )
            return tl
        return await self._with_retry(_do, label="plan_timeline")

    # B-roll cutaway types that actually show the CREATOR'S hands (so they
    # must match her skin tone / nails / wrist jewellery).
    _HANDS_BROLL_TYPES = frozenset({
        "hands_using", "demo_steps", "texture_pour", "unboxing", "before_after",
    })

    async def write_broll_prompt(
        self, *, brief: str, broll_shot: str, visual_note: str,
        product_ref_url: str | None,
        character: "CharacterDescription | None" = None,
        broll_type: str | None = None,
        shows_creator: bool = False,
    ) -> str:
        """Write a Nano Banana prompt for a B-roll cutaway anchor frame.

        B-roll has NO face — it's a product / hands / scene shot. When a
        product reference is provided it's passed as an image at generation
        time (the prompt instructs faithful reproduction). When the cutaway
        shows the creator's HANDS (hands_using/demo_steps/…), ``character`` is
        threaded so the hands match her skin tone / jewellery (the character
        ref image is also attached upstream with a hands-only citation).
        """
        skill_md = self._load_image_skill_md()
        product_clause = (
            " A product reference image is provided — reproduce the product's "
            "exact shape, colour, material, and any branding FAITHFULLY. In "
            "YOUR prompt, refer to it ONLY as 'the product' — DO NOT name its "
            "form or type, DO NOT describe its colour, label artwork, or "
            "branding, and DO NOT show multiple variants side-by-side. The "
            "model copies all of that from the reference — and must NOT invent "
            "any logo, label, or text the reference does not show. Make it a "
            "CLEAN, SHARP, in-focus product shot: crisp focus, NO glare or "
            "reflections, a bright light-coloured background for close-ups, any "
            "brand label facing the camera and legible, and NOT mirrored or "
            "flipped. If the packaging is transparent, glass, or clear "
            "see-through, keep the label and all printed text rendered flat and "
            "adhered ON its surface (printed on the container, never floating "
            "inside it or hovering off it). PHYSICAL LOGIC: show the product in the natural resting "
            "state its form supports (a steady hold, a slow turn to the label, "
            "a natural drape or fold for apparel) exactly as the reference "
            "shows it — never mime an action the product's form can't do, and "
            "for a sealed container keep it closed unless a cap-off is shown "
            "first."
            if product_ref_url else ""
        )
        # A product shot (product_closeup/product_in_scene) that shows the
        # creator shows her HANDS, never her face — so it earns the same
        # creator-hand discipline as the explicit hands b-roll. Without this a
        # product_closeup rendered a stranger's hand (a man's hairy hand on the
        # bottle, run v2-20260614-001634).
        _PRODUCT_HAND_TYPES = {"product_closeup", "product_in_scene"}
        _hand_in_frame = (
            character is not None and shows_creator
            and (broll_type in self._HANDS_BROLL_TYPES
                 or broll_type in _PRODUCT_HAND_TYPES)
        )
        hands_clause = ""
        if _hand_in_frame:
            fem = (character.gender or "").lower().startswith("f")
            hand_desc = "a woman's hand" if fem else "a man's hand"
            not_desc = (
                "It is NOT a man's hand — no broad/large male hands and no hair "
                "on the fingers or knuckles" if fem else
                "It is NOT a woman's hand"
            )
            hands_clause = (
                f" The hand(s) in frame are the CREATOR'S OWN: {hand_desc}, "
                f"{character.skin_tone} skin, natural well-groomed nails, "
                f"matching her wardrobe ({character.wardrobe}) and any wrist "
                f"jewellery/sleeves. {not_desc}. Show ONLY hands/forearms, "
                f"NEVER the face."
            )
        user_prompt = (
            "Write a Nano Banana image prompt for a B-ROLL CUTAWAY frame in "
            "a UGC ad — NO person's face is the subject (hands are fine). "
            "This is a product / detail / scene shot the video cuts to while "
            "the voiceover continues.\n\n"
            "Apply the real-phone-photo realism rules from the system "
            "instruction (natural iPhone capture, no studio/cinematic look, "
            "no UI chrome/overlays, full-bleed photo). Vertical 9:16.\n\n"
            f"BRIEF / PRODUCT:\n{brief}\n\n"
            f"B-ROLL SHOT: {broll_shot}\n"
            f"ON SCREEN: {visual_note}{product_clause}{hands_clause}\n\n"
            "Output: the image prompt as plain text only (no JSON, no "
            "markdown, no preamble)."
        )
        async def _do():
            out = await self._generate(
                tier="fast", prompt=user_prompt, schema=None,
                system_instruction=skill_md, temperature=0.6,
            )
            return out.strip()
        return await self._with_retry(_do, label="write_broll_prompt")

    async def describe_character(self, *, brief: str) -> CharacterDescription:
        """Cast the UGC creator from the BRIEF alone (runs BEFORE the director).

        Derives the creator purely from the brief so the SCENE-FIRST director
        (plan_timeline) can stage every visual_note for this person. No script
        dependency — casting precedes scripting.
        """
        archetype_block = "\n".join(
            f"  - {key}: {desc}"
            for key, desc in CREATOR_ARCHETYPES.items()
        )
        prompt = (
            "Given the ad brief below, write a hyperrealistic UGC "
            "creator character description — a real Indian Instagram-style "
            "content creator from a metro like Mumbai, Delhi or Bangalore. "
            "Naturally photogenic, age-appropriate.\n\n"
            "STEP 1 — CHOOSE THE ARCHETYPE that fits the product:\n"
            f"{archetype_block}\n\n"
            "Pick whichever archetype best fits the product/brief; use "
            "'default' only when nothing else fits. Build the character "
            "AROUND the archetype description — wardrobe, energy, hair "
            "style, makeup level, body language should all reflect it.\n\n"
            "STEP 2 — FILL THE FIELDS:\n"
            "  - ETHNICITY: 'Indian' unless the brief explicitly asks for a "
            "different look.\n"
            "  - GENDER: pick whatever fits the brief best (male or female).\n"
            "  - REGION (structured): pick the Indian regional cluster that "
            "fits the brand audience. VARY this across briefs; do not "
            "default to north_indian.\n"
            "  - SKIN_TONE (structured): pick from the 5 buckets. Indian "
            "creators span the FULL range and EVERY bucket is equally valid "
            "for any brand tier — there is NO default bucket. Unless the "
            "brief specifies, treat all five as live options and pick the "
            "one that makes THIS character distinct (a run of casts should "
            "cover the whole range, not cluster in one or two buckets).\n"
            "  - HAIR_STYLE (structured): pick from the menu. VARY across "
            "briefs — DO NOT default to messy_high_bun. For men, only the "
            "men_* values are valid.\n"
            "  - SETTING (structured): pick what fits the product category "
            "(kitchen for food, balcony/living for lifestyle, gym corner "
            "for fitness, bathroom for skincare, desk for tech).\n"
            "  - EXPRESSION (structured): pick what fits the brief energy "
            "(candid_mid_laugh for hooks, tasting_reaction for food, "
            "focused_engaged for tech/skincare, calm_mid_sentence for "
            "talking-head dialogue).\n"
            "  - HAIR (free-text c.hair): elaborate on the structured "
            "hair_style with colour/length/texture specifics.\n"
            "  - WARDROBE (c.wardrobe): a specific outfit that fits the "
            "archetype — name the pieces concretely (e.g., 'fitted "
            "cream-beige ribbed cotton tank, delicate gold heart pendant, "
            "small gold huggie hoops'). AVOID generic 'casual outfit' / "
            "'traditional Indian wear' (unless festival context). If you "
            "include eyewear, default to sunglasses WORN PUSHED UP ON THE HEAD "
            "(not over the eyes) so the face stays unobstructed for lipsync.\n"
            "  - ENERGY (c.energy): one short phrase capturing the body-"
            "language vibe ('confident wellness energy', 'warm kitchen-"
            "comfortable energy', 'polished beauty-creator energy').\n"
            "  - FACE (c.face): face specifics beyond skin tone — eye "
            "shape, brow, lip, septum/nose-ring, light stubble (for men). "
            "AFFIRMATIVE language only. Do NOT mention pores, peach fuzz, "
            "freckles, moles, micro-asymmetry — these bias the image model "
            "toward aged or Western-coded output (Indians rarely have "
            "freckles).\n"
            "  - MALE characters: include natural facial-hair detail when "
            "relevant (clean-shaven / light stubble / well-groomed short "
            "beard) in c.face.\n"
            "  - VOICE_VIBE (c.voice_vibe): pick the voice energy that fits "
            "the brief from this curated catalog. (gender, vibe) deterministically "
            "maps to a concrete ElevenLabs voice_id, so pick honestly — the "
            "VO will sound exactly as the vibe description suggests.\n\n"
            f"{catalog_for_prompt()}\n\n"
            "  - VOICE_PERSONA (c.voice_persona): the character's speaking "
            "voice for NATIVE Seedance audio — gender (match), age_band, "
            "timbre, pace, warmth that fit the archetype. Accent is fixed "
            "(polished neutral urban Indian-English); do NOT restate it.\n\n"
            "Return JSON matching CharacterDescription.\n\n"
            f"BRIEF:\n{brief}"
        )
        async def _do():
            # High temperature ON PURPOSE (2026-07-31): at the 0.4 default the
            # caster collapsed onto one look (same skin bucket, same hair,
            # same wardrobe) run after run — casting is a creative draw across
            # the whole attribute space, and the structured schema keeps the
            # output valid at high temperature.
            return await self._generate(
                tier="fast", prompt=prompt, schema=CharacterDescription,
                temperature=0.9,
            )
        return await self._with_retry(_do, label="describe_character")

    async def match_voice(
        self, *, character: "CharacterDescription", profiles: list,
    ) -> "VoiceMatch":
        """Pick the cloned voice that best MATCHES the already-cast character.

        Character-first: the character (gender, age, energy, personality,
        voice_persona) is fixed by the brief; this only chooses which real
        cloned voice best FITS it. Candidates are pre-filtered to the
        character's gender by the caller. The model may return voice_id=None
        when NO candidate genuinely fits (e.g. the character reads clearly
        older/younger than every candidate) — the caller then falls back to
        the curated catalog. Returns a VoiceMatch (voice_id + reason).
        """
        if not profiles:
            return VoiceMatch(voice_id=None, reason="no candidate voices available")
        p = character.voice_persona
        want = (
            f"gender={character.gender}; age={p.age_band}; "
            f"energy={character.energy}; timbre={p.timbre}; pace={p.pace}; "
            f"warmth={p.warmth}; vibe={character.voice_vibe}"
        )
        catalog = "\n".join(f"  {i+1}. {vp.prompt_line()}"
                            for i, vp in enumerate(profiles))
        prompt = (
            "Choose the ONE real cloned voice that best MATCHES this character "
            "so the voice the viewer hears fits the person they see. The "
            "character is already cast (do NOT try to change it) — you are only "
            "matching a voice to it.\n\n"
            f"CHARACTER'S EXPECTED VOICE:\n  {want}\n\n"
            "CANDIDATE VOICES (all pre-filtered to the character's gender):\n"
            f"{catalog}\n\n"
            "Weigh age-band closeness FIRST (a young voice on an older "
            "character, or vice-versa, is the exact mismatch to avoid), then "
            "energy, timbre/pace, and overall vibe. Return the chosen voice's "
            "voice_id (the value in [brackets]) and a one-sentence reason. If "
            "NO candidate is a genuinely good fit — especially if the "
            "character's age-band is far from every candidate's — return "
            "voice_id=null so a curated fallback voice can be used instead."
        )
        valid_ids = {vp.voice_id for vp in profiles}

        async def _do(prev_err=None):
            m = await self._generate(
                tier="fast", prompt=prompt, schema=VoiceMatch, temperature=0.2)
            # Guard against a hallucinated id — treat as "no fit" (fallback).
            if m.voice_id and m.voice_id not in valid_ids:
                m.voice_id = None
                m.reason = f"(model returned an unknown id) {m.reason}"
            return m
        return await self._with_retry(_do, label="match_voice")

    async def should_open_with_product(
        self, *, brief: str, opening_chunk_action: str,
    ) -> bool:
        """Decide whether the product belongs in the opening frame.

        Uses structured Pydantic output (ProductInFirstFrameDecision) for
        reliable parsing — no brittle text matching, no surprise outputs.
        """
        prompt = (
            "Given an ad brief and the opening visual action of the first "
            "video chunk, decide: should the product be visible IN THE FIRST "
            "FRAME of the video, or should the opening shot feature only "
            "the creator (with the product appearing later in the scene)?\n\n"
            "Guidelines:\n"
            "  - If the brief is product-forward (demo, unboxing, review, "
            "tutorial) OR the opening action explicitly involves the "
            "product, set in_first_frame=true.\n"
            "  - If the brief leads with a hook, story, or pain-point and "
            "the product reveal lands mid-clip, set in_first_frame=false.\n"
            "  - Default to false when uncertain — opening WITHOUT the "
            "product creates more natural narrative pacing for UGC ads.\n\n"
            "Provide a one-sentence reasoning before deciding.\n\n"
            f"BRIEF:\n{brief}\n\nOPENING ACTION:\n{opening_chunk_action}"
        )
        async def _do():
            # A one-bit decision — the lite tier is plenty.
            decision = await self._generate(
                tier="lite", prompt=prompt,
                schema=ProductInFirstFrameDecision, temperature=0.1,
            )
            return decision.in_first_frame
        try:
            return await self._with_retry(
                _do, label="should_open_with_product",
            )
        except Exception:
            # Safe default when the call or parse fails — story-led opening.
            return False

    def _load_skill_md(self) -> str:
        if self._skill_md_cache is not None:
            return self._skill_md_cache
        if self._skill_md_path is None:
            raise RuntimeError("skill_md_path is required for write_seedance_prompt.")
        self._skill_md_cache = Path(self._skill_md_path).read_text(encoding="utf-8")
        return self._skill_md_cache

    def _load_omni_skill_md(self) -> str:
        if self._omni_skill_md_cache is not None:
            return self._omni_skill_md_cache
        if self._omni_skill_md_path is None:
            raise RuntimeError(
                "omni_skill_md_path is required for omni-mode motion prompts."
            )
        self._omni_skill_md_cache = Path(
            self._omni_skill_md_path
        ).read_text(encoding="utf-8")
        return self._omni_skill_md_cache

    def _load_image_skill_md(self) -> str:
        if self._image_skill_md_cache is not None:
            return self._image_skill_md_cache
        if self._image_skill_md_path is None:
            raise RuntimeError(
                "image_skill_md_path is required for write_character_prompt "
                "and write_first_frame_prompt."
            )
        self._image_skill_md_cache = Path(
            self._image_skill_md_path
        ).read_text(encoding="utf-8")
        return self._image_skill_md_cache

    async def write_character_prompt(
        self, *, brief: str, character: CharacterDescription,
    ) -> str:
        """Write a Nano Banana image prompt for the canonical character
        reference image — a NEUTRAL identity portrait used to lock the
        character across all subsequent anchor frame generations.

        IMPORTANT: This is NOT a chunk's opening frame. There must be no
        chunk-specific action, no held object, no mid-sentence gesture.
        The pose is a composed medium shot looking at camera so future
        anchor generations (which pass this image as a reference input)
        get a clean read on face / hair / wardrobe / jewelry.

        Uses Gemini Flash with the skill MD as system_instruction. Returns a
        plain-text image prompt (no JSON wrapping).
        """
        skill_md = self._load_image_skill_md()
        user_prompt = (
            "Write a Nano Banana image prompt for the CANONICAL CHARACTER "
            "REFERENCE image of a UGC ad. This is NOT a chunk opening "
            "frame — this is the identity anchor used to lock the "
            "character across all subsequent anchor generations.\n\n"
            "CRITICAL POSE OVERRIDE for the character reference:\n"
            "  - NO chunk-specific action — the character is not holding "
            "anything, not mid-bite, not mid-laugh, not gesturing.\n"
            "  - Composed medium shot looking calmly at the camera with a "
            "natural soft expression (slight closed-mouth smile or calm "
            "neutral). Hands relaxed, arms at sides or one hand resting "
            "naturally — NOT performing any action.\n"
            "  - FRAMING is 'tripod' regardless of any chunk framing — "
            "a locked, static eye-level medium shot. Do NOT render the "
            "recording device: no phone, tripod, gimbal, ring light or selfie "
            "stick in frame — the camera is the unseen point of view.\n"
            "  - Background should be a clean tasteful creator backdrop "
            "(blank wall or simple neutral setting) so future anchor "
            "frames can render different scenes without conflicting "
            "background pulls from this reference.\n\n"
            "FACE UNOBSTRUCTED: this is a face-on-camera shot — the full "
            "face is visible, eyes clear. Any sunglasses/aviators are pushed "
            "UP onto the head (never over the eyes); no hat brim or hood low "
            "over the eyes; nothing covering the face.\n\n"
            "SKIN — PRIORITIZE FLATTERING BEAUTY (overrides the system "
            "instruction's imperfection emphasis): render CLEAN, smooth, "
            f"even-toned, {_skin_look(character.gender, character.skin_tone)} skin. Keep it non-plastic "
            "(not airbrushed/poreless) but use only the MOST SUBTLE natural "
            "realism — at most one faint asymmetry and a gentle subsurface "
            "glow. Do NOT emphasize visible pores, blemishes, spots, "
            "blotchiness, redness, oiliness, or multiple 'imperfections'. This "
            "reference frame feeds a VIDEO re-render that amplifies skin "
            "texture into grime, so err toward smooth-and-attractive, never "
            "textured-and-dirty. Keep the creator polish — expressed through "
            "THIS character's own hair/makeup/jewelry choices, not one house "
            "look — and the anatomy footer.\n\n"
            f"BRIEF / PRODUCT:\n{brief}\n\n"
            f"CHARACTER:\n"
            f"  age: {character.age}\n"
            f"  gender: {character.gender}\n"
            f"  ethnicity: {character.ethnicity}\n"
            f"  region: {character.region}\n"
            f"  skin_tone: {character.skin_tone}\n"
            f"  hair_style: {character.hair_style}\n"
            f"  setting: {character.setting}\n"
            f"  expression: {character.expression}\n"
            f"  hair: {character.hair}\n"
            f"  face: {character.face}\n"
            f"  wardrobe: {character.wardrobe}\n"
            f"  energy: {character.energy}\n\n"
            "Output: the image prompt as plain text only (no JSON, no "
            "markdown, no preamble)."
        )
        async def _do():
            out = await self._generate(
                tier="fast", prompt=user_prompt, schema=None,
                system_instruction=skill_md, temperature=0.7,
            )
            return out.strip()
        return await self._with_retry(
            _do, label="write_character_prompt",
        )

    async def write_first_frame_prompt(
        self, *, brief: str, character: CharacterDescription,
        opening_chunk_action: str, framing: str,
        product_in_first_frame: bool, product_ref_url: str | None,
    ) -> str:
        """Write a Nano Banana image prompt for the opening-frame composition.

        Same recipe as the character prompt, but anchored to the first chunk's
        opening visual action + framing convention. When the product is
        meant to be in the first frame, it's worked into the composition.
        """
        skill_md = self._load_image_skill_md()
        product_clause = (
            f"\n\nPRODUCT IN FIRST FRAME: YES — visually compose the "
            f"product (the user uploaded a reference image at "
            f"{product_ref_url}) naturally into the opening shot. The "
            f"creator may be holding it, gesturing toward it, or it sits "
            f"in the frame appropriate to the action.\n"
            f"PRODUCT FIDELITY (CRITICAL): in YOUR prompt, refer to the "
            f"product ONLY as 'the product' — DO NOT name its form or type, "
            f"DO NOT name a variant, DO NOT describe its colour, label, font, "
            f"or artwork, and DO NOT show multiple variants. The model will "
            f"copy the exact form, shape, colour, material, and any branding "
            f"from the reference image (and must NOT invent a logo or text the "
            f"reference doesn't show) — your prompt only describes how the "
            f"product is positioned/held in the scene, never what it looks "
            f"like."
            if product_in_first_frame and product_ref_url else
            "\n\nPRODUCT IN FIRST FRAME: NO — the opening shot shows only "
            "the creator. The product is NOT visible in the first frame; "
            "it will appear later in the chunk's action."
        )
        user_prompt = (
            "Write a Nano Banana image prompt for the OPENING-FRAME image "
            "of a UGC ad video. This image becomes the first visible frame "
            "the audience sees when the video starts. Follow the framing-"
            "appropriate camera stack from §3g, BUT for skin PRIORITIZE "
            "FLATTERING BEAUTY over the system instruction's imperfection "
            f"emphasis: CLEAN, smooth, even-toned, {_skin_look(character.gender, character.skin_tone)} "
            "skin — non-plastic but with only the most subtle realism (at most "
            "one faint asymmetry + gentle subsurface glow); do NOT emphasize "
            "visible pores, blemishes, spots, blotchiness or redness. This "
            "frame feeds a video re-render that amplifies texture into grime, "
            "so err smooth-and-attractive, never textured-and-dirty.\n\n"
            "FACE UNOBSTRUCTED: this is a face-on-camera shot that gets "
            "lip-synced — the full face is visible, eyes clear. Any "
            "sunglasses/aviators are pushed UP onto the head (never over the "
            "eyes); no hat brim or hood low over the eyes; nothing covering "
            "the face.\n\n"
            f"BRIEF / PRODUCT:\n{brief}\n\n"
            f"CHARACTER:\n"
            f"  age: {character.age}\n"
            f"  gender: {character.gender}\n"
            f"  ethnicity: {character.ethnicity}\n"
            f"  region: {character.region}\n"
            f"  skin_tone: {character.skin_tone}\n"
            f"  hair_style: {character.hair_style}\n"
            f"  setting: {character.setting}\n"
            f"  expression: {character.expression}\n"
            f"  hair: {character.hair}\n"
            f"  face: {character.face}\n"
            f"  wardrobe: {character.wardrobe}\n"
            f"  energy: {character.energy}\n\n"
            f"OPENING VISUAL ACTION (what should be happening at t=0):\n"
            f"  {opening_chunk_action}\n\n"
            f"FRAMING CONVENTION: {framing} — "
            f"{_FRAMING_CAMERA_HINT.get(framing, _FRAMING_CAMERA_HINT['selfie'])}\n"
            "CAMERA RIG — CRITICAL: this is the phone's OWN point of view. NEVER "
            "render the recording device in the frame — no phone, tripod, gimbal, "
            "ring light, selfie stick, or a hand holding a phone is visible. The "
            "camera is unseen. (ONLY exception: the 'mirror' framing, where the "
            "phone appears in the mirror reflection by design.)"
            f"{product_clause}\n\n"
            "Output: the image prompt as plain text only (no JSON, no "
            "markdown, no preamble)."
        )
        async def _do():
            out = await self._generate(
                tier="fast", prompt=user_prompt, schema=None,
                system_instruction=skill_md, temperature=0.7,
            )
            return out.strip()
        return await self._with_retry(
            _do, label="write_first_frame_prompt",
        )

    async def write_seedance_prompt(
        self, *, chunk: ChunkSpec, character: CharacterDescription,
        prev_summary: str | None,
        asset_indices: dict[str, int] | None = None,
    ) -> str:
        """Compose the final per-chunk Seedance prompt.

        asset_indices: maps logical slot names ("character_image",
        "product_image", "first_frame_image", "prev_video") to 1-based
        positional indices in the Seedance request, so the Pro prompt can
        emit explicit "Image 1 (the woman)" / "Video 1 (previous chunk)"
        style references. When None, Pro falls back to free-form referencing.
        """
        skill_md = self._load_skill_md()

        char_block = (
            f"Character (locked across all chunks):\n"
            f"  age: {character.age}, gender: {character.gender}, "
            f"ethnicity: {character.ethnicity}\n"
            f"  hair: {character.hair}\n  face: {character.face}\n"
            f"  wardrobe: {character.wardrobe}\n  energy: {character.energy}"
        )

        asset_block = ""
        if asset_indices:
            lines = ["ATTACHED ASSETS (use these positional references in the prompt verbatim):"]
            if "character_image" in asset_indices:
                lines.append(
                    f"  Image {asset_indices['character_image']} = the "
                    f"character (match this face / hair / wardrobe)"
                )
            if "product_image" in asset_indices:
                lines.append(
                    f"  Image {asset_indices['product_image']} = the "
                    f"product (match its packaging / colour / shape)"
                )
            if "first_frame_image" in asset_indices:
                lines.append(
                    f"  Image {asset_indices['first_frame_image']} = the "
                    f"opening frame for this chunk — the video should "
                    f"START here. Write 'opening frame is Image "
                    f"{asset_indices['first_frame_image']}' inside the prompt."
                )
            if "prev_video" in asset_indices:
                lines.append(
                    f"  Video {asset_indices['prev_video']} = previous "
                    f"chunk's video (motion / pacing reference)"
                )
            asset_block = "\n".join(lines)
        continuity_block = (
            f"This is chunk #{chunk.index}.\n"
            + (f"Previous chunk ended with: {prev_summary}\n" if prev_summary
               else "This is the opening chunk.\n")
            + f"This chunk runs from {chunk.t_start}s to {chunk.t_end}s "
              f"(15-second window).\n"
              f"Dialogue (verbatim): {chunk.dialogue}\n"
              f"Visual action: {chunk.action}\n"
              f"Framing convention: {chunk.framing}"
        )
        user_prompt = (
            "Write a final, ready-to-paste Seedance 2.0 video prompt for the chunk below.\n"
            "Apply ALL the rules in the system instruction (three-section structure, "
            "single camera move per slice, anti-distortion fallback, exactly two arms "
            "and two hands, pose lock to prevent 3-arm hallucination).\n"
            "Do NOT include 'pores / peach fuzz / no beauty filter' style cues — use "
            "the affirmative young-skin language from the character description.\n"
            "Reference attached assets by their positional index "
            "(e.g., 'Image 1 (the woman)', 'opening frame is Image 3', "
            "'pacing follows Video 1'). Do NOT invent asset references that "
            "aren't listed.\n"
            "Return the prompt as plain text only — no commentary, no markdown wrappers.\n\n"
            f"{char_block}\n\n{continuity_block}"
            + (f"\n\n{asset_block}" if asset_block else "")
        )
        async def _do():
            out = await self._generate(
                tier="pro", prompt=user_prompt, schema=None,
                system_instruction=skill_md, temperature=0.5,
            )
            return out.strip()
        return await self._with_retry(_do, label="write_seedance_prompt")

    async def write_motion_prompt(
        self, *, beat: Any, character: CharacterDescription,
        window_len: float, mode: str, uses_product: bool,
        anchor_url: str, timeline_overview: str,
        fallback_example: str, persona_clause: str = "",
        spoken_line: str = "", brief_directives: str = "",
        shot_plan: "list[dict] | None" = None,
    ) -> "MotionPrompt":
        """Opus writes ONE beat's Seedance prompt, grounded in its anchor frame.

        system is TWO cached blocks: [skill_md, timeline overview]. Keeping
        skill_md as its own block makes it byte-identical to the prefix
        write_seedance_prompt sends, so the two methods SHARE one cache entry
        for the (large) seedance skill instead of each re-writing it. The
        timeline overview is identical across the per-beat fan-out, so its
        block caches across beats too. Concatenating the two (the old shape)
        gave motion prompts a distinct prefix and forfeited both. The anchor
        frame rides as a multimodal image (per-beat, uncached user turn).
        Returns a validated MotionPrompt; `restaged` records whether Opus chose
        to deviate from the planner's visual_note. No temperature — Opus drops
        it on the pro tier.
        """
        omni = mode.startswith("omni_")
        # Omni gets its OWN skill block (block 0). The two skills are separate
        # cached prefixes on purpose — mixing them would both break the
        # write_seedance_prompt cache share AND teach Opus the wrong style.
        skill_md = self._load_omni_skill_md() if omni else self._load_skill_md()
        system = [
            skill_md,
            f"=== TIMELINE CONTEXT (every beat in this video — keep motion and "
            f"energy flowing across cuts) ===\n{timeline_overview}",
        ]
        # ``spoken_line`` is the caller's SPOKEN form of the line (audio tags
        # stripped, ALL-CAPS emphasis de-CAPSed, pronunciation respellings
        # applied). It must win wherever the task template quotes the line for a
        # model that generates the speech itself: an [audio tag] left in gets
        # read aloud, a CAPS token gets letter-spelled, and a word without its
        # respelling gets mispronounced — none of it repairable after the render.
        line = (spoken_line or beat.dialogue_text or "").strip()
        # A folded take arrives with its shot plan: [{start, end, label, scene}]
        # built from the screenplay by the caller. Opus still WRITES the prompt —
        # it just may not invent the shots, their order or their timings.
        shot_list = "\n".join(
            f"  [{sh['start']}-{sh['end']}s] {sh['label']}: {sh['scene']}"
            for sh in (shot_plan or [])
        )
        task = _MOTION_MODE_TASK[_MODE_TO_TASK[mode]].format(
            window_len=window_len, line=line, persona_clause=persona_clause,
            n_shots=len(shot_plan or []), shot_list=shot_list)
        char_block = (
            f"Creator (LOCKED — the same person in every beat): {character.age} "
            f"{character.gender}, {character.ethnicity}; hair {character.hair}; "
            f"face {character.face}; wardrobe {character.wardrobe}; setting "
            f"{character.setting}; energy {character.energy}."
        )
        framing = f"/{beat.framing}" if beat.framing else ""
        broll = f", broll {beat.broll_type}" if beat.broll_type else ""
        product = "; product is in frame" if uses_product else ""
        # The framing's camera behaviour (docs/framing-taxonomy.md) is a hard
        # direction, not a suggestion — without it Opus wrote the same selfie
        # camera for every frame and the screenplay variety died in the render.
        cam = _FRAMING_MOTION_CAMERA.get(beat.framing or "", "")
        cam_line = (f"Framing camera (execute this move faithfully, it IS the "
                    f"frame): {cam}.\n" if cam else "")
        # The spoken line as CONTEXT for b-roll cutaways — the visual must
        # serve the words it covers (script↔visual lock). B-roll ONLY:
        # sync_talk prompts must never carry the line at all (Opus could echo
        # it into the Seedance prompt and the source mouth would articulate
        # different words than the lipsync pass corrects to).
        raw_line = (beat.dialogue_text or "").strip()
        line_ctx = (
            f"VO over this cutaway (context ONLY — the visual must SERVE this "
            f"line; never render it as on-screen text and do not quote it as "
            f"speech): “{raw_line}”\n"
            if raw_line and _MODE_TO_TASK.get(mode) == "broll"
            else ""
        )
        # The brief's explicit visual directives (extracted up front) must
        # survive to the FINAL prompt writer — the planner alone can't be
        # trusted to have encoded them into every beat field (2026-07-31).
        directives_block = (
            f"BRIEF VISUAL DIRECTIVES (MANDATORY — the client's spec; honor "
            f"any that apply to this shot):\n{brief_directives}\n\n"
            if brief_directives else ""
        )
        if omni:
            user_prompt = (
                "Write the Omni video prompt for ONE beat, applying the craft "
                "rules in the system instruction.\n\n"
                "Image 1 is the ALREADY-RENDERED opening frame for THIS beat. "
                "Declare its role explicitly (exact character/product "
                "reference, preserve face, hair, wardrobe, setting) and then "
                "write the FULL creative brief: scene, action, camera, "
                "lighting, audio.\n\n"
                f"{char_block}\n\n"
                f"{directives_block}"
                f"THIS beat #{beat.index} — section {beat.ad_section}, shot "
                f"{beat.shot_type}{framing}{broll}{product}. Clip is "
                f"{window_len:.1f}s.\n"
                f"{cam_line}"
                f"Planner's staging (visual_note): {beat.visual_note}\n"
                "Honor this staging by DEFAULT. Re-stage or change the action "
                "ONLY if Image 1 contradicts it or the planned action would "
                "look unnatural — otherwise keep the planner's intent. Set "
                "'restaged' accordingly.\n\n"
                f"{task}\n\n"
                + ("Follow the six-dimension structure and include every "
                   "mandatory verbatim token from the system instruction — the "
                   "voice clause exactly as given. The single-unbroken-shot lock "
                   "is the ONE token you must NOT include: this beat cuts.\n\n"
                   if shot_plan else
                   "Follow the six-dimension structure and include every "
                   "mandatory verbatim token from the system instruction — "
                   "especially the single-unbroken-shot lock and the voice clause "
                   "exactly as given.\n\n") +
                "A baseline prompt for this beat type is below — BEAT it, do "
                f"not copy it:\n---\n{fallback_example}\n---"
            )
        else:
            user_prompt = (
            "Write the Seedance video prompt for ONE beat, applying the craft "
            "rules in the system instruction.\n\n"
            "Image 1 is the ALREADY-RENDERED opening frame for THIS beat. The "
            "scene, setting, wardrobe, props and creator identity are LOCKED by "
            "it. Do NOT re-describe them — direct only MOTION, CAMERA and AUDIO. "
            "Re-describing what is already in frame makes the model morph it.\n\n"
            f"{char_block}\n\n"
            f"{directives_block}"
            f"THIS beat #{beat.index} — section {beat.ad_section}, shot "
            f"{beat.shot_type}{framing}{broll}{product}. Clip is "
            f"{window_len:.1f}s.\n"
            f"{cam_line}"
            f"{line_ctx}"
            f"Planner's staging (visual_note): {beat.visual_note}\n"
            "Honor this staging by DEFAULT. Re-stage or change the action ONLY "
            "if Image 1 contradicts it or the planned action would look "
            "unnatural / glitchy — otherwise keep the planner's intent; a "
            "restage must still SERVE the VO line above. Set "
            "'restaged' accordingly.\n\n"
            f"{task}\n\n"
            "Be resourceful and dense — every clause earns its place. Apply the "
            "realism + camera/subject + anti-CGI rules from the system "
            "instruction (you decide how to fold them in). Order the prompt the "
            "way Seedance weights it: motion -> camera -> texture/faithfulness "
            "-> a short constraint tail. Seedance down-weights trailing tokens, "
            "so front-load the highest-value direction and do not pad (~60-100 "
            "words is where it is most effective).\n\n"
            "A baseline prompt for this beat type is below — BEAT it, do not "
            f"copy it:\n---\n{fallback_example}\n---"
        )
        async def _do():
            mp = await self._generate(
                tier="pro", prompt=user_prompt, schema=MotionPrompt,
                system_instruction=system, image_url=anchor_url,
            )
            if shot_plan:
                # Opus obeys the skill doc's single-shot lock unless told
                # otherwise, and on the 2026-08-06 live run it rewrote both takes
                # into ordinary single-shot prompts — the clips came back as one
                # uncut 7s/9s shot. So the cut structure is verified, not trusted.
                wanted = [f"[{sh['start']}-{sh['end']}s]" for sh in shot_plan]
                missing = [tag for tag in wanted if tag not in mp.prompt]
                if missing:
                    raise ValueError(
                        f"multi-shot prompt dropped {len(missing)} of "
                        f"{len(wanted)} timecode block(s) — every shot needs its "
                        f"own '[start-ends]' entry, in order. Missing: "
                        f"{' '.join(missing)}"
                    )
                if "single unbroken continuous shot" in mp.prompt:
                    raise ValueError(
                        "multi-shot prompt still contains the "
                        "single-unbroken-shot lock — this beat MUST cut, so that "
                        "token must not appear."
                    )
                # Where the voice comes from, shot by shot, is what makes a
                # cutaway render as a cutaway rather than another talking angle
                # (trial 1 → 2, 2026-08-05). Without it the model reads "she says
                # the line" as "she is on camera for the whole take".
                if (any("CUTAWAY" in sh["label"] for sh in shot_plan)
                        and "voiceover" not in mp.prompt.lower()):
                    raise ValueError(
                        "multi-shot prompt has an INSERT CUTAWAY but never says "
                        "her voice continues as an OFF-CAMERA VOICEOVER over it "
                        "— without that placement the model renders her talking "
                        "on camera through the cutaway instead."
                    )
            return mp
        return await self._with_retry(_do, label="write_motion_prompt")
