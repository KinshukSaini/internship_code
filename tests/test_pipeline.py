"""Unit tests for the pipeline's testable helpers: resilient fan-out, the
paid-fan-out length guard, deterministic seeds, and the motion-prompt diet.

The full generate_ugc_video() orchestration is exercised by the headless smoke
script (scripts/test_pipeline.py); here we lock down the pieces that protect
paid renders so they can't silently regress.
"""
import math
from pathlib import Path

import pytest

from app.pipeline import (
    _MICRO_GESTURES,
    _SECTION_MOTION,
    _STATIC_CAMERA_FRAMINGS,
    _anchor_refs,
    _anchor_visual_note,
    _assert_aligned,
    _broll_motion_prompt,
    _camera_fixed,
    _clip_request,
    _gather_with_retry,
    _native_talking_prompt,
    _native_vo_broll_prompt,
    _native_silent_broll_prompt,
    _normalize_product_ref,
    _seed_for,
    _talking_motion_prompt,
)


@pytest.mark.asyncio
async def test_gather_with_retry_recovers_a_transient_failure():
    """One clip fails on the first attempt, succeeds on retry; siblings keep
    their results and the final list is complete + ordered."""
    state = {"fail_idx1": True}

    async def make(i):
        if i == 1 and state["fail_idx1"]:
            state["fail_idx1"] = False     # fail once, then succeed
            raise RuntimeError("transient blip")
        return f"r{i}"

    msgs: list[str] = []
    res = await _gather_with_retry(
        [lambda i=i: make(i) for i in range(3)],
        label="clip(s)", on_progress=msgs.append,
    )
    assert res == ["r0", "r1", "r2"]
    assert any("retry" in m for m in msgs)


@pytest.mark.asyncio
async def test_gather_with_retry_raises_when_still_failing():
    async def boom(i):
        raise RuntimeError(f"perma-{i}")

    with pytest.raises(RuntimeError, match="permanently failed"):
        await _gather_with_retry(
            [lambda i=i: boom(i) for i in range(2)],
            label="clip(s)", on_progress=lambda _m: None,
        )


@pytest.mark.asyncio
async def test_gather_with_retry_all_succeed_no_retry_message():
    async def ok(i):
        return i * 10

    msgs: list[str] = []
    res = await _gather_with_retry(
        [lambda i=i: ok(i) for i in range(4)],
        label="anchor frame(s)", on_progress=msgs.append,
    )
    assert res == [0, 10, 20, 30]
    assert msgs == []   # no retry chatter when nothing fails


def test_assert_aligned_passes_on_equal_lengths():
    _assert_aligned(beats=[1, 2, 3], anchors=["a", "b", "c"], windows=[(0, 1)] * 3)


def test_assert_aligned_raises_on_mismatch():
    with pytest.raises(RuntimeError, match="length mismatch"):
        _assert_aligned(beats=[1, 2, 3], anchors=["a", "b"])   # 3 vs 2


def test_seed_for_is_deterministic_per_run_and_beat():
    w = Path("/tmp/jobs/workflows/wf-abc123/work")
    assert _seed_for(w, 0) == _seed_for(w, 0)          # deterministic
    assert _seed_for(w, 0) != _seed_for(w, 1)          # varies per beat
    # Different run dir → different seed space.
    assert _seed_for(w, 0) != _seed_for(Path("/tmp/other"), 0)
    # Within Seedance's accepted int range.
    assert 0 <= _seed_for(w, 7) < 2_147_483_647


from types import SimpleNamespace


def _tbeat(ad_section="solution", index=0, visual_note="she smiles and waves",
           framing="selfie", dialogue_text="I love this so much",
           shot_type="talking_head", sub_index=0, broll_type=None,
           broll_shot=None, beat_kind="speech", hold_s=None,
           shows_creator=True):
    return SimpleNamespace(ad_section=ad_section, index=index,
                           visual_note=visual_note, shot_type=shot_type,
                           framing=framing, dialogue_text=dialogue_text,
                           sub_index=sub_index, broll_type=broll_type,
                           broll_shot=broll_shot, beat_kind=beat_kind,
                           hold_s=hold_s, shows_creator=shows_creator)


def test_talking_motion_prompt_is_concise_and_positive():
    p = _talking_motion_prompt(
        _tbeat(visual_note="she smiles and waves at the camera"), 4.0,
    )
    # Director-style i2v prompt with a hard anatomy guard + the skin-texture /
    # faithfulness realism tail (docs/seedance-realism-guide.md) runs longer than
    # the 60-100w t2v ideal by design; the high-value tokens sit in the first ~45
    # words where weight is highest. This bound just guards against runaway bloat.
    # (+5 2026-07-14: anatomy guard grew "defined knuckles / rubbery / no finger
    # points at the lens" after QC caught warped fingers in a live run.)
    # (+7 2026-07-31: lively/energetic presence wording — user reported the
    #  whole video reading slow/sluggish.)
    assert len(p.split()) <= 167, f"talking prompt too long: {len(p.split())} words"
    assert "iPhone front camera" in p
    # Scene/wardrobe lives in the anchor image — re-describing it in the motion
    # prompt makes Seedance pop props in / morph, so visual_note is absent.
    assert "she smiles and waves at the camera" not in p
    # The diet converted the wall of negations to positives.
    assert "NOT" not in p
    # Anti-frozen / real-time intent is still present (positively phrased).
    assert "real-time" in p and "continuous" in p


def test_talking_motion_prompt_budgets_gestures_to_the_window():
    """The clip is ordered at ceil(window) but trimmed to the window — the
    prompt must tell Seedance to land gestures inside the kept span, without
    introducing a settle/freeze tail (that kills sync-3)."""
    p = _talking_motion_prompt(_tbeat(), 3.5)
    assert "within the first 3.5 seconds" in p
    assert "settles" not in p                # no settle tail on talking heads
    # sync-3 needs GENTLE mouth motion to correct against — exaggerated source
    # flapping bleeds through the mouth re-render as mismatched articulation
    # (QC hit, run v2-20260714-142306 beat 0).
    assert "subtle natural mouth movement" in p


def test_talking_motion_prompt_camera_follows_framing():
    """The camera line must follow the beat's FRAMING (docs/framing-taxonomy.md)
    — one hard-coded selfie camera for every framing was a top cause of
    same-looking videos. Legacy front-cam framings keep the iPhone default."""
    floor = _talking_motion_prompt(_tbeat(framing="floor_shot"), 3.0)
    assert "floor" in floor and "phone footage" in floor
    assert "iPhone front camera" not in floor
    pan = _talking_motion_prompt(_tbeat(framing="pan_right"), 3.0)
    assert "pan" in pan.lower() and "left to right" in pan
    selfie = _talking_motion_prompt(_tbeat(framing="selfie"), 3.0)
    assert "iPhone front camera" in selfie


def test_talking_motion_prompt_varies_by_section_and_index():
    """Consecutive talking heads must NOT all get identical motion (the old
    single template caused repetitive head-bobs)."""
    hook = _talking_motion_prompt(_tbeat("hook", 0), 4.0)
    cta = _talking_motion_prompt(_tbeat("cta", 1), 4.0)
    assert hook != cta                       # different section tone
    # Two beats in the SAME section but different index still differ (rotated
    # micro-gesture).
    sol0 = _talking_motion_prompt(_tbeat("solution", 2), 4.0)
    sol1 = _talking_motion_prompt(_tbeat("solution", 3), 4.0)
    assert sol0 != sol1


def test_native_talking_prompt_quotes_the_stripped_line():
    """Seedance-native mode: the dialogue rides IN the prompt, in quotes
    (video-seedance-skill.md rule), with [audio tags] stripped — tags are
    delivery instructions, not speech — and the audio-binding sentence uses
    TIMBRE reference (not content-follow). ≤100 words per the skill, even
    with a realistic line."""
    beat = _tbeat(
        dialogue_text="[laughs] Instead, I just fixed one small thing. "
                      "My breakfast.",
        visual_note="she holds the pack up to the camera and smiles",
    )
    p = _native_talking_prompt(beat)
    assert '"Instead, I just fixed one small thing. My breakfast."' in p
    assert "[laughs]" not in p
    # Scene/wardrobe lives in the anchor image, not the motion prompt — so the
    # visual_note must NOT appear here (re-describing it morphs props).
    assert "she holds the pack up to the camera and smiles" not in p
    # TIMBRE reference, NOT content-follow
    assert "voice timbre references Audio 1" in p
    assert "follow Audio 1" not in p
    # The dialogue line rides IN the prompt and the skin-texture tail was added
    # for realism (docs/seedance-realism-guide.md), so the budget is wider than
    # the bare-skill 100w ideal; still bounded to catch runaway bloat.
    # (+8 2026-07-14: widened anatomy guard, see talking-prompt bound note.)
    # (+4 2026-07-29: added the director's VOCAL delivery direction — native mode
    #  had no voice-emotion direction at all, which is why the read was flat. Paid
    #  for by trimming the duplicated "says exactly" in _FIDELITY_CLAUSE and the
    #  speech-rate half of _NATIVE_PACE_CLAUSE, which the direction now owns.)
    # (+8 2026-07-31: _FAITHFULNESS_CLAUSE wired in — it was verified missing
    #  from this builder, letting native talking clips reframe/recolor away
    #  from their anchor.)
    assert len(p.split()) <= 190, f"native prompt too long: {len(p.split())} words"


def test_native_talking_prompt_has_no_syncso_mouth_clause():
    """The 'mouth clearly opening and closing' clause exists ONLY to feed
    sync-3 — in audio-driven mode it would fight the reference audio. Same
    for the numeric gesture-window budget: the reference audio paces the
    take and the native trim runs to the end of speech, not a window cut.
    The timbre-reference model does not use 'real-time' pacing text either."""
    p = _native_talking_prompt(_tbeat())
    assert "mouth clearly opening and closing" not in p
    assert "seconds" not in p                # no gesture-window budget
    assert "voice timbre references Audio 1" in p   # timbre reference, not content-follow
    assert "Exactly one pair of hands" in p  # anatomy clause kept


def test_native_talking_prompt_varies_by_section():
    hook = _native_talking_prompt(_tbeat("hook"))
    cta = _native_talking_prompt(_tbeat("cta"))
    assert hook != cta


def test_broll_motion_prompt_is_concise_and_positive():
    p = _broll_motion_prompt(
        "macro of the product on a counter", "a drop forms", "lifestyle",
    )
    assert len(p.split()) <= 142  # + realism tail + anatomy guard + energy (2026-07-31)
    assert "NOT" not in p
    assert "macro of the product on a counter" in p
    # Settle clause reworked 2026-07-31: real-time energy through the clip,
    # never a frozen final second (user reported "stuck" shots).
    assert "real-time energy" in p
    assert "settles into a calm" not in p


def test_broll_motion_prompt_bans_phantom_hands_and_morphing():
    """Product/hands b-roll carries the one-pair-of-hands / no-phantom-limb ban
    AND the product-stability ban (grip + label hold, no morph) — the two top
    Seedance warp sources on a held branded product."""
    p = _broll_motion_prompt(
        "hands holding the box", "she turns it to the lens", "hands_using",
    )
    low = p.lower()
    assert "one pair of hands" in low
    assert "phantom" in low
    assert "morphs or warps" in low or "morphing or warping" in low
    assert "grip stays constant" in low
    # size-lock + label-adhesion (transparent-box fix, 2026-07-22)
    assert "rescale" in low and "printed flat" in low


def test_broll_motion_prompt_camera_matches_camera_fixed():
    """Product/hands b-roll takes a slow controlled PUSH-IN (never a hard lock —
    a locked camera over a static product froze the clip / spawned a phantom
    hand); lifestyle b-roll keeps the handheld feel. Neither uses a locked-off
    tripod, so both agree with _camera_fixed=False for b-roll."""
    from app.pipeline import _PRODUCT_HANDS_BROLL_TYPES
    for bt in _PRODUCT_HANDS_BROLL_TYPES:
        p = _broll_motion_prompt("a shot", "she does a thing", bt).lower()
        assert "push-in" in p, bt
        assert "locked-off tripod" not in p, bt
        assert "handheld" not in p, bt
    life = _broll_motion_prompt("a shot", "she does a thing", "lifestyle").lower()
    assert "handheld" in life
    assert "locked-off tripod" not in life
    assert "push-in" not in life


def test_anatomy_clause_forbids_extra_hands():
    """The shared anatomy clause (on every motion prompt) must forbid extra /
    duplicate / phantom hands, not merely ask for 'two hands'."""
    from app.pipeline import _ANATOMY_CLAUSE
    low = _ANATOMY_CLAUSE.lower()
    assert "one pair of hands" in low
    assert "phantom" in low
    assert "caress" not in low and "stroke" not in low


def test_prompts_carry_skin_texture_defense():
    """The single biggest cause of the plastic look was that the skin-texture /
    optics tail was specced but never wired into the live prompts
    (docs/seedance-realism-guide.md). Frames 2..N are re-synthesized and regress
    to a smooth prior unless texture is NAMED, so every generated path must carry
    it — and it must be phrased POSITIVELY (Seedance has no negative_prompt field;
    'no plastic skin' can backfire). Product b-roll is the deliberate exception:
    film grain dirties a clean label, so it gets material-realism instead."""
    talk = _talking_motion_prompt(_tbeat(), 4.0)
    nat = _native_talking_prompt(_tbeat())
    life = _broll_motion_prompt("a shot", "she does a thing", "lifestyle")
    prod = _broll_motion_prompt("a shot", "she does a thing", "hands_using")

    for p in (talk, nat, life):
        assert "fine natural texture" in p
        assert "subsurface scattering" in p
    # Flattering, not gritty: these are attractive influencers, so the texture
    # defense must NOT lean on pore/oil/peach-fuzz emphasis that reads blotchy.
    for p in (talk, nat, life):
        assert "T-zone" not in p and "vellus" not in p
        assert "attractive skin" in p
        # Anti-sweat: the clause must forbid the oily/shiny sheen that made a
        # young male creator read as sweaty (run v2-20260714-012336).
        assert "never" in p and "oily" in p and "sweaty" in p
        assert "dewy" not in p.lower()
    # Positive-only: never negate the subject noun (would surface the artifact).
    for p in (talk, nat, life, prod):
        assert "no plastic skin" not in p.lower()
        assert "waxy" not in p.lower()
    # First-frame faithfulness on the silent-i2v + b-roll paths (anti-morph).
    assert "Preserve the composition and colors" in talk
    assert "Preserve the composition and colors" in life
    # Product/hero b-roll: NO grain (label stays clean), material realism instead.
    assert "film grain" not in prod
    assert "material reflections" in prod


def test_camera_never_locks_broll():
    """B-roll NEVER sends camera_fixed now — product/hands shots take a slow
    controlled push-in instead of a hard lock (the lock froze the footage and
    spawned a phantom hand); lifestyle keeps handheld. Only static-rig talking
    heads still lock."""
    from app.pipeline import _PRODUCT_HANDS_BROLL_TYPES
    assert _PRODUCT_HANDS_BROLL_TYPES == frozenset(
        {"product_closeup", "product_in_scene", "hands_using"}
    )
    for bt in list(_PRODUCT_HANDS_BROLL_TYPES) + ["lifestyle", None]:
        assert _camera_fixed(
            _tbeat(shot_type="broll", framing=None, broll_type=bt)
        ) is False, bt


def test_dur_max_is_tightened():
    """Beats are ≤5s, so the SOFT cap is 6s; the API ceiling stays 15s."""
    from app.pipeline import _DUR_API_MAX, _DUR_MAX, _DUR_MIN
    assert _DUR_MIN == 4
    assert _DUR_MAX == 6
    assert _DUR_API_MAX == 15


def test_requested_duration_always_covers_window():
    """THE BUG (beat-3 crash): a 7.44s window must request an 8s clip, never
    get clamped to the 6s soft cap — a clip shorter than its own window is a
    guaranteed assembly failure after every sibling has been billed."""
    from app.pipeline import _requested_duration
    assert _requested_duration(7.44) == 8     # covers the window (was 6 → crash)
    assert _requested_duration(3.2) == 4      # API floor
    assert _requested_duration(4.0) == 4
    assert _requested_duration(4.6) == 5
    assert _requested_duration(20.0) == 15    # API ceiling


def test_clip_request_native_routes_to_r2v_with_camera_unlocked():
    """Native talking heads go to reference-to-video (the only EvoLink model
    that accepts audio_urls) with the voice baked in — and never send
    camera_fixed, even for a tripod framing: r2v's acceptance of the key is
    unverified, and a 400 would kill a fully-billed clip stage."""
    from app.config import NATIVE_LIPSYNC_MODEL
    req = _clip_request(
        _tbeat(framing="tripod"), anchor_url="https://a/x.png", duration=5,
        seed=7, native=True, prompt="p", audio_url="https://au/ref.wav",
    )
    assert req.model == NATIVE_LIPSYNC_MODEL
    assert req.camera_fixed is False
    assert req.generate_audio is True
    assert req.audio_url == "https://au/ref.wav"


def test_clip_request_default_model_for_syncso_and_broll():
    """Non-native beats stay on the default i2v model, silent, with the
    tripod-only camera lock intact."""
    from app.video_gen import DEFAULT_MODEL_ID
    th = _clip_request(
        _tbeat(framing="tripod"), anchor_url="https://a/x.png", duration=5,
        seed=7, native=False, prompt="p", audio_url=None,
    )
    assert th.model == DEFAULT_MODEL_ID
    assert th.camera_fixed is True            # tripod lock preserved
    assert th.generate_audio is False
    assert th.audio_url is None
    broll = _clip_request(
        _tbeat(shot_type="broll", framing=None), anchor_url="https://a/x.png",
        duration=4, seed=8, native=False, prompt="p", audio_url=None,
    )
    assert broll.model == DEFAULT_MODEL_ID
    assert broll.camera_fixed is False
    assert broll.generate_audio is False


def test_clip_request_resolution_threads_to_quality():
    """clip_resolution flows to VideoRequest.resolution (the EvoLink `quality`
    field) — default 720p, 1080p opt-in."""
    default = _clip_request(
        _tbeat(framing="tripod"), anchor_url="https://a/x.png", duration=5,
        seed=7, native=False, prompt="p", audio_url=None,
    )
    assert default.resolution == "720p"
    hires = _clip_request(
        _tbeat(framing="tripod"), anchor_url="https://a/x.png", duration=5,
        seed=7, native=False, prompt="p", audio_url=None, resolution="1080p",
    )
    assert hires.resolution == "1080p"


def test_camera_fixed_only_for_static_rig_talking_heads():
    """Lock the camera ONLY for talking heads on a physically RESTING rig
    (tripod + the taxonomy's floor/table/overhead frames, 2026-07-31) — every
    handheld/moving framing keeps its sway or move (the UGC tell), and pans /
    follows / drops must never lock (the move IS the frame)."""
    assert "tripod" in _STATIC_CAMERA_FRAMINGS
    assert "floor_shot" in _STATIC_CAMERA_FRAMINGS
    assert "top_angle" in _STATIC_CAMERA_FRAMINGS
    for moving in ("pan_right", "pan_left", "high_angle_drop", "follow_turn",
                   "side_track", "vlog", "pov_walk"):
        assert moving not in _STATIC_CAMERA_FRAMINGS, moving
    assert _camera_fixed(_tbeat(framing="tripod")) is True
    assert _camera_fixed(_tbeat(framing="low_angle_table")) is True
    assert _camera_fixed(_tbeat(framing="selfie")) is False
    assert _camera_fixed(_tbeat(framing="pan_right")) is False
    assert _camera_fixed(_tbeat(framing="lav_mic")) is False
    # B-roll never locks, even with a stray framing value.
    assert _camera_fixed(_tbeat(shot_type="broll", framing="tripod")) is False


def test_anchor_visual_note_marks_jump_cut_continuations():
    """Sub-beats after the first (sub_index>0 from split_long_windows) must
    NOT reset to the parent's opening pose — identical anchors make every
    jump cut snap back to the same frame."""
    first = _anchor_visual_note(_tbeat(visual_note="she waves", sub_index=0))
    assert first == "she waves"
    cont = _anchor_visual_note(_tbeat(visual_note="she waves", sub_index=1))
    assert "she waves" in cont
    assert "mid-take continuation" in cont
    assert "NOT the opening pose" in cont
    # Beats without the marker (unsplit timelines) behave like sub_index=0.
    bare = SimpleNamespace(visual_note="she waves")
    assert _anchor_visual_note(bare) == "she waves"


def test_pipeline_entrypoint_exposes_engine_and_pacing_params():
    import inspect

    from app.pipeline import generate_ugc_video
    params = inspect.signature(generate_ugc_video).parameters
    assert params["lipsync_engine"].default == "syncso"
    assert params["trim_silence"].default is True


@pytest.mark.asyncio
async def test_generate_ugc_video_rejects_unknown_engine(tmp_path):
    """A typo'd engine must fail before any client is built or paid call
    made — settings=None proves nothing else was touched."""
    from app.pipeline import generate_ugc_video
    with pytest.raises(ValueError, match="lipsync_engine"):
        await generate_ugc_video(
            brief="b", settings=None, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", lipsync_engine="bogus",
        )


def test_generate_ugc_video_granularity_default_is_per_clip():
    import inspect

    from app.pipeline import generate_ugc_video
    params = inspect.signature(generate_ugc_video).parameters
    assert params["lipsync_granularity"].default == "per_clip"


@pytest.mark.asyncio
async def test_generate_ugc_video_rejects_unknown_granularity(tmp_path):
    from app.pipeline import generate_ugc_video
    with pytest.raises(ValueError, match="lipsync_granularity"):
        await generate_ugc_video(
            brief="b", settings=None, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", lipsync_granularity="bogus",
        )


@pytest.mark.asyncio
async def test_generate_ugc_video_rejects_whole_video_on_non_syncso(tmp_path):
    """whole-video lipsync is Sync.so-only — pairing it with heygen must fail
    fast, before any client is built or paid call is made."""
    from app.pipeline import generate_ugc_video
    with pytest.raises(ValueError, match="only supported with"):
        await generate_ugc_video(
            brief="b", settings=None, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4",
            lipsync_engine="heygen", lipsync_granularity="whole",
        )


@pytest.mark.asyncio
async def test_generate_ugc_video_rejects_unknown_voice_provider(tmp_path):
    """Provider validation is pure-argument, so it must fire before `settings`
    is touched (settings=None here) and before any paid call."""
    from app.pipeline import generate_ugc_video
    with pytest.raises(ValueError, match="unknown voice_provider"):
        await generate_ugc_video(
            brief="b", settings=None, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", voice_provider="nope",
        )


@pytest.mark.asyncio
async def test_generate_ugc_video_requires_elevenlabs_key_on_every_provider(
        tmp_path):
    """ElevenLabs supplies the char-level master clock (forced alignment) and
    the Scribe QC transcription on EVERY provider, so a missing key must fail
    up front rather than after the first paid Cartesia/Fish TTS."""
    from app.config import Settings
    from app.pipeline import generate_ugc_video
    s = Settings(
        evolink_api_key="e", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="g",
        elevenlabs_api_key="", sync_api_key="sy", anthropic_api_key="a",
        cartesia_api_key="c", fish_api_key="f",
    )
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        await generate_ugc_video(
            brief="b", settings=s, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", voice_provider="cartesia",
        )


# Hands are the #1 Seedance warp source, so motion must stay face/head only.
_HAND_PHRASES = ("hand", "flourish", "gesture", "shoulders")


def test_section_motion_carries_no_hand_gestures():
    """Each delivery tone must be ONE head/face/expression directive — stacking
    verbs or prompting hands morphs the clip."""
    for section, tone in _SECTION_MOTION.items():
        low = tone.lower()
        for phrase in _HAND_PHRASES:
            assert phrase not in low, f"{section!r} tone still mentions {phrase!r}"


def test_micro_gestures_are_face_head_only():
    """The rotated micro-move must never reach for the hands (dropped the old
    'hand flourish near the chest')."""
    for micro in _MICRO_GESTURES:
        low = micro.lower()
        assert "hand" not in low and "flourish" not in low, micro
    # One-shoulder shrug stays (a head/upper-body micro-move, not a hand move).
    assert any("shrug" in m for m in _MICRO_GESTURES)


_CHAR_REF = "https://cdn/char.png"
_PROD_REF = "https://cdn/product.png"


def test_anchor_refs_talking_head_gets_character():
    refs, roles = _anchor_refs(
        _tbeat(shot_type="talking_head"), _CHAR_REF, None,
    )
    assert refs == [_CHAR_REF]
    assert roles == ["character"]


def test_anchor_refs_lifestyle_broll_with_creator_gets_character():
    """A person-in-scene (lifestyle) B-roll where the creator is on screen
    cites the full character ref — IDENTITY rides shows_creator, not shot_type."""
    refs, roles = _anchor_refs(
        _tbeat(shot_type="broll", framing=None, broll_type="lifestyle",
               shows_creator=True),
        _CHAR_REF, None,
    )
    assert refs == [_CHAR_REF]
    assert roles == ["character"]


def test_anchor_refs_product_macro_gets_no_character():
    """A pure product macro (shows_creator=False) attaches ONLY the product —
    never the character, so the model can't invent a stranger's face/body."""
    refs, roles = _anchor_refs(
        _tbeat(shot_type="broll", framing=None, broll_type="product_closeup",
               shows_creator=False),
        _CHAR_REF, _PROD_REF,
    )
    assert refs == [_PROD_REF]
    assert roles == ["product"]
    assert "character" not in roles


def test_anchor_refs_hands_broll_cites_hands_only():
    """A hands-on-product cutaway with the creator on screen cites the hands
    role (so the model matches her hands but never renders her face) plus the
    product."""
    refs, roles = _anchor_refs(
        _tbeat(shot_type="broll", framing=None, broll_type="hands_using",
               shows_creator=True),
        _CHAR_REF, _PROD_REF,
    )
    assert refs == [_CHAR_REF, _PROD_REF]
    assert roles == ["character_hands", "product"]


@pytest.mark.parametrize("bt", ["product_closeup", "product_in_scene"])
def test_anchor_refs_product_shot_with_creator_cites_hands_only(bt):
    """A product_closeup/product_in_scene where the creator is on screen shows
    her HAND holding the product, never her face — so it cites the HANDS role,
    not full 'character'. Regression for v2-20260614-001634, where a
    product_closeup fell to the face-lock branch and rendered a man's hand."""
    refs, roles = _anchor_refs(
        _tbeat(shot_type="broll", framing=None, broll_type=bt,
               shows_creator=True),
        _CHAR_REF, _PROD_REF,
    )
    assert refs == [_CHAR_REF, _PROD_REF]
    assert roles == ["character_hands", "product"]


class _FakeImg:
    def __init__(self, url):
        self.url = url


class _FakeImageClient:
    """Records generate_async calls so the normalization seam can be asserted
    without any network/Gemini call."""
    def __init__(self, urls=None, fail_times=0):
        self._urls = urls or ["https://cdn/normalized.png"]
        self._fail_times = fail_times
        self.calls: list[dict] = []

    async def generate_async(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("transient gemini blip")
        return [_FakeImg(self._urls[0])]


@pytest.mark.asyncio
async def test_normalize_product_ref_edits_via_product_source_role():
    """The user's photo is passed as a reference EDIT cited as product_source,
    and the clean bottle URL is returned for downstream anchors to pull from."""
    client = _FakeImageClient(urls=["https://cdn/clean-bottle.png"])
    url = await _normalize_product_ref(
        client, "https://cdn/user-photo.png", on_progress=lambda _m: None,
    )
    assert url == "https://cdn/clean-bottle.png"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["reference_roles"] == ["product_source"]
    assert call["reference_image_urls"] == ["https://cdn/user-photo.png"]
    assert call["tier"] == "hero"


@pytest.mark.asyncio
async def test_normalize_product_ref_retries_once_then_succeeds():
    client = _FakeImageClient(urls=["https://cdn/clean.png"], fail_times=1)
    msgs: list[str] = []
    url = await _normalize_product_ref(
        client, "https://cdn/orig.png", on_progress=msgs.append,
    )
    assert url == "https://cdn/clean.png"
    assert len(client.calls) == 2          # failed once, retried once
    assert any("failed" in m for m in msgs)


@pytest.mark.asyncio
async def test_normalize_product_ref_returns_none_on_permanent_failure():
    """Both attempts fail → None, so the caller falls back to the original
    photo rather than killing the (paid) run."""
    client = _FakeImageClient(fail_times=5)
    url = await _normalize_product_ref(
        client, "https://cdn/orig.png", on_progress=lambda _m: None,
    )
    assert url is None
    assert len(client.calls) == 2          # one retry, no more


# --- generate_ugc_video wiring (fakes replace every paid client) -----------

class _Rec(SimpleNamespace):
    """SimpleNamespace that satisfies the pipeline's .model_dump() calls."""
    def model_dump(self):
        return {k: v for k, v in self.__dict__.items()}


class _RecGemini:
    """Records call ORDER + kwargs so describe_character→plan_timeline order
    and the character/product_present hand-off can be asserted."""
    def __init__(self, calls, timeline):
        self._calls = calls
        self._timeline = timeline

    async def describe_character(self, *, brief):
        self._calls.append(("describe_character", {"brief": brief}))
        # voice_persona is required by _write_motion_prompts when lipsync_engine
        # == "seedance_native" (render_voice_persona(character.voice_persona)).
        from types import SimpleNamespace
        voice_persona = SimpleNamespace(
            gender="female", age_band="mid-20s", timbre="warm bright",
            pace="calm", warmth="friendly",
        )
        return _Rec(
            gender="female", voice_vibe="warm", age="20s", ethnicity="x",
            region="x", skin_tone="x", hair_style="x", setting="x",
            expression="x", hair="x", face="x", wardrobe="x", energy="x",
            voice_persona=voice_persona,
        )

    async def plan_timeline(self, **kwargs):
        self._calls.append(("plan_timeline", kwargs))
        return self._timeline

    async def match_voice(self, *, character, profiles):
        # These tests assert the CATALOG fallback (voice_for → "voice-x"), so
        # the stub reports "no cloned voice fits" → caller falls back.
        self._calls.append(("match_voice", {"n": len(profiles)}))
        from app.gemini import VoiceMatch
        return VoiceMatch(voice_id=None, reason="stub: no fit")

    async def write_character_prompt(self, **kwargs):
        return "char ref prompt"

    async def write_first_frame_prompt(self, **kwargs):
        return "ff prompt"

    async def write_broll_prompt(self, **kwargs):
        return "broll prompt"


class _FakeVoice:
    # voice_vibe is part of the uniform provider contract (Cartesia maps it to
    # generation_config.emotion; ElevenLabs accepts and ignores it), so every
    # fake voice client must accept it.
    async def generate_vo(self, *, script, voice_id, out_path, voice_vibe=None):
        from app.voice import Alignment, VoiceResult
        Path(out_path).write_bytes(b"")
        n = max(1, len(script))
        align = Alignment(
            characters=list(script.ljust(n)[:n]),
            starts=[float(i) for i in range(n)],
            ends=[float(i + 1) for i in range(n)],
        )
        return VoiceResult(audio_path=str(out_path), alignment=align)


class _FakeUploader:
    def upload(self, local_path):
        return f"https://cdn/{Path(local_path).name}"


class _FakeAnchorClient:
    def __init__(self, calls):
        self._calls = calls

    async def generate_async(self, **kwargs):
        self._calls.append(("anchor_img", kwargs))
        return [_FakeImg("https://cdn/anchor.png")]


class _FakeVideoClient:
    async def create_task_async(self, request):
        return "task-1", {}

    async def poll_task_stream_async(self, task_id):
        yield "done", SimpleNamespace(video_url="https://cdn/clip.mp4")


def _beat(**ov):
    d = dict(index=0, ad_section="solution", dialogue_text="hello there friend",
             shot_type="talking_head", framing="selfie", broll_type=None,
             broll_shot=None, visual_note="she talks", beat_kind="speech",
             hold_s=None, shows_creator=True, sub_index=0, voice_mode=None,
             product_in_frame=False)
    d.update(ov)
    return SimpleNamespace(**d)


@pytest.mark.asyncio
async def test_generate_ugc_video_wires_director_holds_and_native_padding(
    tmp_path, monkeypatch,
):
    """End-to-end wiring with every paid client faked:
      - describe_character runs BEFORE plan_timeline and the director receives
        character + product_present;
      - apply_holds is called with the post-window beats and its outputs feed
        the clip + assemble stages;
      - a native talking-head reference is padded to _requested_duration.
    """
    import app.pipeline as P
    from app.config import Settings

    calls: list = []

    speech = _beat(index=0, dialogue_text="hello there friend")
    reaction = _beat(index=1, dialogue_text="", beat_kind="reaction",
                     hold_s=0.8, shot_type="broll", framing=None,
                     broll_type="lifestyle", shows_creator=True)
    timeline = _Rec(
        full_script="hello there friend", target_duration_s=None,
        script_source="generated", hook_type="bold_claim",
        narrative_structure="problem_solution",
        beats=[speech, reaction],
    )

    monkeypatch.setattr(P, "GeminiClient",
                        lambda *a, **k: _RecGemini(calls, timeline))
    monkeypatch.setattr(P, "make_voice_client", lambda s, provider=None: _FakeVoice())
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_image_client",
                        lambda s, u: _FakeAnchorClient(calls))
    monkeypatch.setattr(P, "make_video_client", lambda s, vg=None: _FakeVideoClient())
    monkeypatch.setattr(P, "make_lipsync_client", lambda s, e=None: object())
    monkeypatch.setattr(P, "voice_for", lambda g, v: "voice-x")

    # Windowing is exercised in test_assembly; here pin deterministic windows
    # so the holds + padding wiring is the only thing under test.
    monkeypatch.setattr(P, "beat_windows",
                        lambda beats, al, fs: [(0.0, 2.0), (2.0, 2.0)])
    monkeypatch.setattr(P, "split_long_windows",
                        lambda beats, w, al: (list(beats), list(w)))
    monkeypatch.setattr(P, "merge_short_windows",
                        lambda beats, w: (list(beats), list(w)))

    holds_calls: list = []

    def _fake_apply_holds(beats, windows, alignment, vo, *, work_dir):
        holds_calls.append({"beats": beats, "windows": list(windows)})
        # Returns a NEW vo path + shifted windows so downstream use is provable.
        new_vo = str(Path(work_dir) / "vo-holds.wav")
        Path(new_vo).write_bytes(b"")
        return new_vo, alignment, [(0.0, 2.0), (2.0, 2.8)]
    monkeypatch.setattr(P, "apply_holds", _fake_apply_holds)

    pad_calls: list = []
    monkeypatch.setattr(
        P, "pad_audio_to",
        lambda src, target_s, dest: (
            pad_calls.append({"src": str(src), "target_s": target_s,
                              "dest": str(dest)}),
            Path(dest).write_bytes(b""),
        ),
    )
    monkeypatch.setattr(P, "slice_audio",
                        lambda src, a, b, dest: Path(dest).write_bytes(b""))
    monkeypatch.setattr(P, "download",
                        lambda url, local: Path(local).write_bytes(b""))

    assemble_calls: list = []

    async def _fake_assemble(**kwargs):
        assemble_calls.append(kwargs)
        return SimpleNamespace(path=str(tmp_path / "out.mp4"),
                               duration_s=2.8, n_lipsynced=1)
    monkeypatch.setattr(P, "assemble", _fake_assemble)

    settings = Settings(
        gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
    )
    result = await P.generate_ugc_video(
        brief="sell the thing", settings=settings, work_dir=tmp_path / "work",
        out_path=tmp_path / "out.mp4", lipsync_engine="seedance_native",
        llm_provider="gemini", trim_silence=False,
        on_progress=lambda _m: None,
    )

    # describe_character precedes the director, which receives character +
    # product_present.
    names = [c[0] for c in calls if c[0] in ("describe_character",
                                             "plan_timeline")]
    assert names.index("describe_character") < names.index("plan_timeline")
    plan_kwargs = next(c[1] for c in calls if c[0] == "plan_timeline")
    assert plan_kwargs["product_present"] is False        # no product_ref_url
    assert plan_kwargs["character"] is not None

    # apply_holds ran AFTER windowing (got 2 beats + 2 windows) and its shifted
    # windows flowed into assemble.
    assert len(holds_calls) == 1
    assert len(holds_calls[0]["windows"]) == 2
    assert assemble_calls[0]["windows"] == [(0.0, 2.0), (2.0, 2.8)]
    assert assemble_calls[0]["vo_audio_path"].endswith("vo-holds.wav")

    # Native-timbre mode: the shared anchor is built once from the VO head — no
    # per-beat slicing+padding happens. pad_audio_to must NOT have been called
    # (that was the old per-beat path; the anchor upload replaces it).
    assert pad_calls == [], (
        "per-beat audio padding must NOT happen in native-timbre mode — "
        "the shared timbre anchor is used instead"
    )
    assert result.duration_s == 2.8

    # --- per-step prompt/output ledger + S3 deliverable URLs -------------
    import json as _json
    manifest = _json.loads(
        (tmp_path / "work" / "manifest.json").read_text())
    steps = manifest["steps"]
    by_step: dict = {}
    for s in steps:
        by_step.setdefault(s["step"], []).append(s)

    # major steps are all logged, in order, with character before timeline.
    order = [s["step"] for s in steps]
    assert order.index("character") < order.index("timeline")
    for name in ("character", "timeline", "voiceover", "char_ref_image",
                 "anchor", "motion", "clip", "final"):
        assert name in by_step, f"missing step {name!r}"

    # generation prompts are captured (the gaps we set out to close).
    assert by_step["char_ref_image"][0]["prompt"]
    assert all(a["prompt"] for a in by_step["anchor"])
    assert all(m["prompt"] for m in by_step["motion"])

    # deliverables carry their S3 URL (from _FakeUploader -> https://cdn/...).
    assert manifest["vo_s3_url"].startswith("https://cdn/")
    assert manifest["final_video_s3_url"].startswith("https://cdn/")
    assert by_step["final"][0]["produced"]["s3_url"].startswith("https://cdn/")
    assert all(c["produced"]["s3_url"].startswith("https://cdn/")
               for c in by_step["clip"])


# --- Task 4: pure helpers — mode, fallback, timeline overview --------------

def test_beat_mode_classifies_every_beat_kind():
    from app.pipeline import _beat_mode
    # seedance_native: delegates to resolve_voice_mode — talking head with
    # dialogue → on_camera, broll with dialogue → background_vo, no dialogue → silent.
    assert _beat_mode(_tbeat(shot_type="talking_head",
                             dialogue_text="hello there friend"),
                      lipsync_engine="seedance_native") == "on_camera"
    assert _beat_mode(_tbeat(shot_type="broll", framing=None,
                             dialogue_text="over this shot"),
                      lipsync_engine="seedance_native") == "background_vo"
    assert _beat_mode(_tbeat(shot_type="broll", framing=None,
                             dialogue_text=""),
                      lipsync_engine="seedance_native") == "silent"
    # non-seedance engine: talking head → sync_talk
    assert _beat_mode(_tbeat(shot_type="talking_head"),
                      lipsync_engine="syncso") == "sync_talk"
    # broll under syncso stays broll
    assert _beat_mode(_tbeat(shot_type="broll", framing=None),
                      lipsync_engine="syncso") == "broll"
    # a reaction beat is never audio-driven, regardless of engine
    assert _beat_mode(_tbeat(shot_type="talking_head", beat_kind="reaction"),
                      lipsync_engine="seedance_native") == "reaction"


def test_beat_mode_broll_under_syncso_is_broll():
    """A b-roll beat is a cutaway under every engine — never sync_talk."""
    from app.pipeline import _beat_mode
    assert _beat_mode(_tbeat(shot_type="broll", framing=None),
                      lipsync_engine="syncso") == "broll"
    # A reaction b-roll is still classified reaction (kind wins over shot_type).
    assert _beat_mode(_tbeat(shot_type="broll", framing=None,
                             beat_kind="reaction"),
                      lipsync_engine="syncso") == "reaction"


def test_beat_mode_defaults_missing_beat_kind_to_speech():
    """A beat with no beat_kind attribute (legacy/unsplit) defaults to speech,
    so it is never mis-classified as a reaction."""
    from app.pipeline import _beat_mode
    bare = SimpleNamespace(shot_type="talking_head")
    assert _beat_mode(bare, lipsync_engine="syncso") == "sync_talk"


def test_fallback_prompt_matches_the_clip_path():
    from app.pipeline import (_fallback_prompt, _talking_motion_prompt,
                              _broll_motion_prompt)
    b = _tbeat(shot_type="talking_head")
    assert _fallback_prompt(b, 4.0, lipsync_engine="syncso") == \
        _talking_motion_prompt(b, 4.0)
    bb = _tbeat(shot_type="broll", framing=None, broll_type="lifestyle",
                broll_shot="a shot", visual_note="she pours")
    assert _fallback_prompt(bb, 4.0, lipsync_engine="syncso") == \
        _broll_motion_prompt("a shot", "she pours", "lifestyle")


def test_fallback_prompt_native_talk_uses_native_builder():
    """Under seedance_native a talking head's fallback is the audio-driven
    f-string (line in quotes), not the sync.so one."""
    from app.pipeline import _fallback_prompt, _native_talking_prompt
    b = _tbeat(shot_type="talking_head", dialogue_text="okay so listen")
    assert _fallback_prompt(b, 4.0, lipsync_engine="seedance_native") == \
        _native_talking_prompt(b)


def test_fallback_prompt_reaction_uses_silent_broll_builder():
    """A reaction beat shares the silent b-roll f-string today — the fallback
    must route it through _broll_motion_prompt, not a talking builder."""
    from app.pipeline import _fallback_prompt, _broll_motion_prompt
    b = _tbeat(shot_type="talking_head", beat_kind="reaction",
               dialogue_text="", broll_shot=None, broll_type=None,
               visual_note="she raises an eyebrow")
    assert _fallback_prompt(b, 2.0, lipsync_engine="syncso") == \
        _broll_motion_prompt("", "she raises an eyebrow", None)


def test_timeline_overview_lists_every_beat_with_section_and_line():
    from app.pipeline import _timeline_overview
    beats = [_tbeat(index=0, ad_section="hook", dialogue_text="okay so"),
             _tbeat(index=1, ad_section="cta", shot_type="broll",
                    framing=None, dialogue_text="")]
    ov = _timeline_overview(beats)
    assert "beat 0" in ov and "hook" in ov and "okay so" in ov
    assert "beat 1" in ov and "cta" in ov
    assert "(silent)" in ov          # empty dialogue rendered explicitly


def test_timeline_overview_renders_one_line_per_beat_with_kind():
    """One line per beat, carrying section/shot/kind so Opus sees the whole arc
    — and a None dialogue is treated like an empty one (silent)."""
    from app.pipeline import _timeline_overview
    beats = [
        _tbeat(index=0, ad_section="hook", shot_type="talking_head",
               beat_kind="speech", dialogue_text="hey"),
        _tbeat(index=1, ad_section="proof", shot_type="broll", framing=None,
               beat_kind="reaction", dialogue_text=None),
    ]
    ov = _timeline_overview(beats)
    lines = [ln for ln in ov.splitlines() if ln.strip().startswith("beat ")]
    assert len(lines) == 2
    assert "[hook/talking_head/speech]" in lines[0]
    assert "[proof/broll/reaction]" in lines[1]
    assert "(silent)" in lines[1]    # None dialogue → silent, no crash


def test_timeline_overview_truncates_long_dialogue():
    """Long lines are clipped (to ~80 chars) so the shared prefix stays compact
    across the per-beat fan-out."""
    from app.pipeline import _timeline_overview
    long = "x" * 200
    ov = _timeline_overview([_tbeat(index=0, dialogue_text=long)])
    beat_line = next(ln for ln in ov.splitlines()
                     if ln.strip().startswith("beat 0"))
    # The 200-char line must not be carried in full.
    assert long not in beat_line
    assert beat_line.count("x") == 80


# --- Task 5: _write_motion_prompts stage (Opus + fallback + manifest) ------

class _FakeManifest:
    def __init__(self):
        self.data = {}

    def update(self, **fields):
        self.data.update(fields)


def _anchor(url):
    return SimpleNamespace(url=url)


@pytest.mark.asyncio
async def test_write_motion_prompts_opus_path_and_records(monkeypatch):
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    calls = []

    async def fake_write(**kw):
        calls.append(kw)
        return MotionPrompt(prompt=f"OPUS-{kw['beat'].index}", restaged=True)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beats = [_tbeat(index=0, shot_type="talking_head"),
             _tbeat(index=1, shot_type="broll", framing=None,
                    broll_type="lifestyle", broll_shot="a shot")]
    anchors = [_anchor("u0"), _anchor("u1")]
    windows = [(0.0, 3.0), (3.0, 6.0)]
    man = _FakeManifest()

    recs = await _write_motion_prompts(
        beats=beats, anchors=anchors, windows=windows, gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=man, on_asset=None,
    )
    assert recs[0]["source"] == "opus"
    # Opus prompts that lack the anti-warp core get the anatomy guard
    # appended verbatim (never left to paraphrase).
    assert recs[0]["prompt"].startswith("OPUS-0")
    assert "never stretched" in recs[0]["prompt"]
    assert recs[0]["restaged"] is True and recs[0]["mode"] == "sync_talk"
    assert recs[1]["mode"] == "broll" and recs[1]["source"] == "opus"
    assert man.data["motion_prompts"][0]["index"] == 0       # persisted
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_write_motion_prompts_record_is_debug_rich(monkeypatch):
    """The persisted record carries every debug field the spec promises:
    index / mode / source / prompt / anchor_url / planner_visual_note /
    restaged / model / provider / negative_prompt — so a bad sample is fully
    diagnosable from the manifest, and the clip stage (and a resume) can read
    the video PROVIDER back off the record instead of re-deriving it."""
    from app.pipeline import _write_motion_prompts
    from app.config import CLAUDE_PRO_MODEL
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt="P", restaged=True)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beat = _tbeat(index=0, shot_type="talking_head",
                  visual_note="she leans into the lens")
    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("anchor-0")], windows=[(0.0, 4.0)],
        gem=gem, character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    rec = recs[0]
    assert set(rec) == {"index", "mode", "source", "prompt", "anchor_url",
                        "planner_visual_note", "restaged", "model",
                        "provider", "negative_prompt"}
    assert rec["index"] == 0
    assert rec["mode"] == "sync_talk"
    assert rec["source"] == "opus"
    assert rec["prompt"].startswith("P")
    assert rec["anchor_url"] == "anchor-0"
    assert rec["planner_visual_note"] == "she leans into the lens"
    assert rec["restaged"] is True
    assert rec["model"] == CLAUDE_PRO_MODEL
    # Default provider: Kling is opt-in, so an unconfigured run stays Seedance
    # and carries no negative_prompt (Seedance has no such field).
    assert rec["provider"] == "seedance"
    assert rec["negative_prompt"] is None


@pytest.mark.asyncio
async def test_write_motion_prompts_constrains_fine_motor_beats():
    """A fine-motor staging (tying laces) is KEPT — never rejected (user
    correction 2026-08-01) — but its motion prompt must carry the
    cut-on-action constraint (_FINE_MOTOR_CLAUSE) so the warp-prone
    multi-step finger work is not animated. Applied on the Opus path (guard
    re-append) and present on the fallback path too."""
    from app.pipeline import _FINE_MOTOR_CLAUSE, _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt="hands move over the laces",
                            restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    tying = _tbeat(index=0, shot_type="broll", framing=None,
                   broll_type="hands_using",
                   broll_shot="she is tying the laces of the white sneakers",
                   visual_note="close on her hands at the shoe")
    plain = _tbeat(index=1, shot_type="broll", framing=None,
                   broll_type="lifestyle", broll_shot="she sips her coffee",
                   visual_note="morning light")
    recs = await _write_motion_prompts(
        beats=[tying, plain], anchors=[_anchor("u0"), _anchor("u1")],
        windows=[(0.0, 3.0), (3.0, 6.0)], gem=gem, character=object(),
        llm="claude", product_ref_url=None, lipsync_engine="syncso",
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    assert _FINE_MOTOR_CLAUSE in recs[0]["prompt"]      # constrained
    assert "multi-step finger" not in recs[1]["prompt"]  # untouched


@pytest.mark.asyncio
async def test_write_motion_prompts_passes_product_camera_mode():
    """A product-bearing b-roll beat is handed to Opus with uses_product=True
    and mode=broll, so the prompt can demand the controlled push-in camera."""
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    seen = {}

    async def fake_write(**kw):
        seen.update(kw)
        return MotionPrompt(prompt="P", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    # product_closeup is a product-bearing broll_type, and a product_ref_url is
    # supplied → _beat_uses_product is True.
    beat = _tbeat(index=0, shot_type="broll", framing=None,
                  broll_type="product_closeup", broll_shot="macro of the box")
    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="claude",
        product_ref_url="https://cdn/product.png",
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert recs[0]["mode"] == "broll"
    assert seen["mode"] == "broll"
    assert seen["uses_product"] is True
    assert seen["anchor_url"] == "u"
    # window_len is the trimmed span, not the requested clip duration.
    assert seen["window_len"] == 4.0


@pytest.mark.asyncio
async def test_write_motion_prompts_fires_on_asset_callback():
    """When an on_asset sink is provided, the stage emits the full ordered
    record list under the 'motion_prompts' key for the manifest UI."""
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt=f"P{kw['beat'].index}", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    emitted = []
    await _write_motion_prompts(
        beats=[_tbeat(index=0, shot_type="talking_head"),
               _tbeat(index=1, shot_type="talking_head")],
        anchors=[_anchor("u0"), _anchor("u1")],
        windows=[(0.0, 4.0), (4.0, 8.0)], gem=gem, character=object(),
        llm="claude", product_ref_url=None, lipsync_engine="syncso",
        on_progress=lambda _m: None, manifest=_FakeManifest(),
        on_asset=lambda key, val: emitted.append((key, val)),
    )
    assert len(emitted) == 1
    key, val = emitted[0]
    assert key == "motion_prompts"
    assert [r["index"] for r in val] == [0, 1]   # ordered


@pytest.mark.asyncio
async def test_write_motion_prompts_falls_back_on_opus_error():
    from app.pipeline import _write_motion_prompts, _talking_motion_prompt

    async def boom(**kw):
        raise RuntimeError("opus down")
    gem = SimpleNamespace(write_motion_prompt=boom)
    beats = [_tbeat(index=0, shot_type="talking_head")]
    msgs: list[str] = []
    recs = await _write_motion_prompts(
        beats=beats, anchors=[_anchor("u0")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=msgs.append,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert recs[0]["source"] == "fallback"
    assert recs[0]["prompt"] == _talking_motion_prompt(beats[0], 4.0)
    # The fallback record carries no model and is not flagged restaged.
    assert recs[0]["model"] is None
    assert recs[0]["restaged"] is False
    # The failure is surfaced on the progress channel (not silently swallowed).
    assert any("fell back" in m for m in msgs)


@pytest.mark.asyncio
async def test_write_motion_prompts_one_failure_does_not_abort_siblings():
    """A single beat's Opus failure must NOT take down the others — that beat
    falls back while its siblings keep their Opus prompts (a paid run never
    dies on one bad sample)."""
    from app.pipeline import _write_motion_prompts, _talking_motion_prompt
    from app.gemini import MotionPrompt

    async def flaky(**kw):
        if kw["beat"].index == 1:
            raise RuntimeError("just this one")
        return MotionPrompt(prompt=f"OPUS-{kw['beat'].index}", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=flaky)

    beats = [_tbeat(index=0, shot_type="talking_head"),
             _tbeat(index=1, shot_type="talking_head"),
             _tbeat(index=2, shot_type="talking_head")]
    recs = await _write_motion_prompts(
        beats=beats, anchors=[_anchor("u0"), _anchor("u1"), _anchor("u2")],
        windows=[(0.0, 4.0), (4.0, 8.0), (8.0, 12.0)], gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert recs[0]["source"] == "opus" and recs[0]["prompt"].startswith("OPUS-0")
    assert recs[1]["source"] == "fallback"
    assert recs[1]["prompt"] == _talking_motion_prompt(beats[1], 4.0)
    assert recs[2]["source"] == "opus" and recs[2]["prompt"].startswith("OPUS-2")


@pytest.mark.asyncio
async def test_write_motion_prompts_skips_opus_for_reaction_and_non_claude():
    from app.pipeline import _write_motion_prompts
    called = {"n": 0}

    async def fake_write(**kw):
        called["n"] += 1
        from app.gemini import MotionPrompt
        return MotionPrompt(prompt="x", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    # reaction beat under claude → skipped
    rec = await _write_motion_prompts(
        beats=[_tbeat(index=0, shot_type="talking_head", beat_kind="reaction",
                      dialogue_text="")],
        anchors=[_anchor("u")], windows=[(0.0, 1.0)], gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert rec[0]["source"] == "reaction" and called["n"] == 0

    # non-claude provider → all fallback, gem never called
    rec2 = await _write_motion_prompts(
        beats=[_tbeat(index=0, shot_type="talking_head")],
        anchors=[_anchor("u")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="gemini", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert rec2[0]["source"] == "fallback" and called["n"] == 0


@pytest.mark.asyncio
async def test_write_motion_prompts_reaction_record_uses_fallback_prompt():
    """A reaction beat skips Opus but still carries a usable f-string prompt
    (the silent b-roll builder) so _clip has something to render."""
    from app.pipeline import _write_motion_prompts, _broll_motion_prompt
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt="should-not-be-used", restaged=True)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beat = _tbeat(index=0, shot_type="talking_head", beat_kind="reaction",
                  dialogue_text="", broll_shot=None, broll_type=None,
                  visual_note="she gasps")
    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u")], windows=[(0.0, 1.5)], gem=gem,
        character=object(), llm="claude", product_ref_url=None,
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert recs[0]["source"] == "reaction"
    assert recs[0]["model"] is None and recs[0]["restaged"] is False
    assert recs[0]["prompt"] == _broll_motion_prompt("", "she gasps", None)


@pytest.mark.asyncio
async def test_write_motion_prompts_rejects_misaligned_inputs():
    """beats / anchors / windows must be the same length — a misalignment is a
    programmer error that must blow up before any (paid) Opus call, not pair the
    wrong anchor with a beat."""
    from app.pipeline import _write_motion_prompts
    called = {"n": 0}

    async def fake_write(**kw):
        called["n"] += 1
        from app.gemini import MotionPrompt
        return MotionPrompt(prompt="x", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    with pytest.raises(RuntimeError, match="length mismatch"):
        await _write_motion_prompts(
            beats=[_tbeat(index=0), _tbeat(index=1)],
            anchors=[_anchor("u0")], windows=[(0.0, 4.0)], gem=gem,
            character=object(), llm="claude", product_ref_url=None,
            lipsync_engine="syncso", on_progress=lambda _m: None,
            manifest=_FakeManifest(), on_asset=None,
        )
    assert called["n"] == 0          # blew up before any Opus call


# --- Task 3: resolve_voice_mode -------------------------------------------

from app.pipeline import resolve_voice_mode


def test_resolve_voice_mode_talking_head_is_on_camera():
    assert resolve_voice_mode(_beat(shot_type="talking_head")) == "on_camera"


def test_resolve_voice_mode_broll_with_dialogue_is_background_vo():
    assert resolve_voice_mode(
        _beat(shot_type="broll", dialogue_text="it changed my skin")) == "background_vo"


def test_resolve_voice_mode_broll_without_dialogue_is_silent():
    assert resolve_voice_mode(
        _beat(shot_type="broll", dialogue_text="  ")) == "silent"


def test_resolve_voice_mode_reaction_is_silent():
    assert resolve_voice_mode(
        _beat(shot_type="talking_head", beat_kind="reaction", dialogue_text="")) == "silent"


def test_resolve_voice_mode_talking_head_no_line_is_silent():
    assert resolve_voice_mode(
        _beat(shot_type="talking_head", dialogue_text="")) == "silent"


def test_resolve_voice_mode_honors_explicit_field():
    assert resolve_voice_mode(
        _beat(shot_type="broll", dialogue_text="x", voice_mode="silent")) == "silent"


# ── build_timbre_anchor ──────────────────────────────────────────────────────

from unittest.mock import MagicMock
from app.pipeline import build_timbre_anchor


def test_build_timbre_anchor_slices_head_and_uploads_once(tmp_path, monkeypatch):
    calls = {"slice": []}
    def fake_slice(src, start, end, dst):
        calls["slice"].append((start, end))
        from pathlib import Path; Path(dst).write_bytes(b"a")
    monkeypatch.setattr("app.pipeline.slice_audio", fake_slice)
    uploader = MagicMock(); uploader.upload.return_value = "https://s3/anchor.wav"

    url = build_timbre_anchor(tmp_path / "vo.mp3", tmp_path, uploader, seconds=7.0)

    assert url == "https://s3/anchor.wav"
    assert calls["slice"] == [(0.0, 7.0)]          # head of the VO
    assert uploader.upload.call_count == 1          # ONE upload, reused downstream


# ---------------------------------------------------------------------------
# Native-timbre prompt builders (Task 5)
# ---------------------------------------------------------------------------

_PERSONA = (
    "The voice is a friendly, warm bright female voice, mid-20s, calm, "
    "polished Indian-English."
)


def test_on_camera_prompt_uses_timbre_reference_not_follow():
    """_native_talking_prompt must use TIMBRE reference (not content-follow),
    include strict fidelity, and embed the dialogue line and persona."""
    b = _tbeat(shot_type="talking_head", dialogue_text="this changed my skin")
    p = _native_talking_prompt(b, _PERSONA)
    assert "voice timbre references Audio 1" in p
    assert "follow Audio 1" not in p           # must NOT be content-follow
    assert "this changed my skin" in p
    assert "says exactly" in p                  # strict fidelity
    assert _PERSONA in p


def test_background_vo_prompt_is_offscreen_voiceover():
    """_native_vo_broll_prompt must produce off-screen voiceover text with
    timbre reference and embed the dialogue line."""
    b = _tbeat(shot_type="broll", dialogue_text="and it absorbs in seconds")
    p = _native_vo_broll_prompt(b, _PERSONA)
    assert "voice timbre references Audio 1" in p or "voiceover timbre references Audio 1" in p
    assert "off-screen" in p.lower() or "voiceover" in p.lower()
    assert "and it absorbs in seconds" in p


def test_silent_broll_prompt_has_no_dialogue_and_no_audio_ref():
    """_native_silent_broll_prompt must contain no Audio 1 reference and must
    contain 'ambient' and 'no speech' phrases."""
    b = _tbeat(shot_type="broll", dialogue_text="")
    p = _native_silent_broll_prompt(b)
    assert "Audio 1" not in p
    assert "ambient" in p.lower() and "no speech" in p.lower()


# --- Task 6: _clip_audio_url sharing contract --------------------------------

from app.pipeline import _clip_audio_url


def test_clip_audio_url_shares_anchor_for_speaking_modes():
    """on_camera and background_vo both get the shared timbre anchor URL;
    silent (reaction / pure broll) gets None so Seedance runs without audio."""
    anchor = "https://s3/anchor.wav"
    assert _clip_audio_url("on_camera", anchor) == anchor
    assert _clip_audio_url("background_vo", anchor) == anchor
    assert _clip_audio_url("silent", anchor) is None
    # reaction maps to None too (it's classified "reaction" not "silent" by
    # _beat_mode, but _clip sets mode first — guard it here for completeness).
    assert _clip_audio_url("reaction", anchor) is None


def test_clip_audio_url_returns_none_when_anchor_is_none():
    """If timbre_anchor_url was never built (non-timbre engine), all modes
    return None — no audio ref is passed to _clip_request."""
    assert _clip_audio_url("on_camera", None) is None
    assert _clip_audio_url("background_vo", None) is None
    assert _clip_audio_url("silent", None) is None


# --- Task 6+7: _write_motion_prompts skips Opus for timbre modes -------------

@pytest.mark.asyncio
async def test_write_motion_prompts_skips_opus_for_native_timbre_modes():
    """Under seedance_native, on_camera / background_vo / silent beats MUST NOT
    call Opus — the deterministic f-string is the final prompt and source is
    'native_timbre'. This guarantees the mandatory timbre+fidelity tokens are
    never paraphrased away."""
    from app.pipeline import _write_motion_prompts
    called = {"n": 0}

    async def must_not_be_called(**kw):
        called["n"] += 1
        from app.gemini import MotionPrompt
        return MotionPrompt(prompt="should-not-appear", restaged=True)
    gem = SimpleNamespace(write_motion_prompt=must_not_be_called)

    # voice_persona required by _write_motion_prompts when seedance_native.
    voice_persona = SimpleNamespace(
        gender="female", age_band="mid-20s", timbre="warm bright",
        pace="calm", warmth="friendly",
    )
    char = SimpleNamespace(voice_persona=voice_persona)

    beats = [
        _tbeat(index=0, shot_type="talking_head",
               dialogue_text="this changed my skin"),   # on_camera
        _tbeat(index=1, shot_type="broll", framing=None,
               broll_type="lifestyle", dialogue_text="and it absorbs fast"),  # background_vo
        _tbeat(index=2, shot_type="broll", framing=None,
               broll_type="lifestyle", dialogue_text=""),  # silent
    ]
    recs = await _write_motion_prompts(
        beats=beats, anchors=[_anchor("u0"), _anchor("u1"), _anchor("u2")],
        windows=[(0.0, 4.0), (4.0, 7.0), (7.0, 10.0)],
        gem=gem, character=char, llm="claude", product_ref_url=None,
        lipsync_engine="seedance_native", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert called["n"] == 0, "Opus must NOT be called for timbre engine modes"
    assert recs[0]["source"] == "native_timbre"
    assert recs[1]["source"] == "native_timbre"
    assert recs[2]["source"] == "native_timbre"
    # Prompts must be the deterministic f-strings, not "should-not-appear".
    assert recs[0]["mode"] == "on_camera"
    assert recs[1]["mode"] == "background_vo"
    assert recs[2]["mode"] == "silent"
    # Each prompt must contain the expected timbre/ambient markers.
    assert "voice timbre references Audio 1" in recs[0]["prompt"]
    assert "Audio 1" not in recs[2]["prompt"]   # silent has no audio ref


@pytest.mark.asyncio
async def test_write_motion_prompts_persona_clause_threaded_into_timbre_prompts():
    """The persona_clause built from character.voice_persona must appear in the
    on_camera / background_vo prompts so Seedance steers the correct voice."""
    from app.pipeline import _write_motion_prompts

    async def must_not_be_called(**kw):
        from app.gemini import MotionPrompt
        return MotionPrompt(prompt="x", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=must_not_be_called)

    voice_persona = SimpleNamespace(
        gender="female", age_band="mid-20s", timbre="warm bright",
        pace="calm", warmth="friendly",
    )
    char = SimpleNamespace(voice_persona=voice_persona)

    beat = _tbeat(index=0, shot_type="talking_head",
                  dialogue_text="this changed everything")
    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u0")], windows=[(0.0, 4.0)],
        gem=gem, character=char, llm="claude", product_ref_url=None,
        lipsync_engine="seedance_native", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    # The rendered persona clause contains the timbre string.
    assert "warm bright" in recs[0]["prompt"]
    assert "friendly" in recs[0]["prompt"]


# --- Task 7: timbre_anchor_url in generate_ugc_video manifest ----------------

@pytest.mark.asyncio
async def test_generate_ugc_video_builds_timbre_anchor_and_records_in_manifest(
    tmp_path, monkeypatch,
):
    """In seedance_native mode the timbre anchor is built once (one
    slice_audio + one upload) and the URL lands in the manifest under
    timbre_anchor_url. Non-timbre engines must NOT call build_timbre_anchor."""
    import app.pipeline as P
    from app.config import Settings

    calls: list = []
    speech = _beat(index=0, dialogue_text="hello there friend")
    timeline = _Rec(
        full_script="hello there friend", target_duration_s=None,
        script_source="generated", hook_type="bold_claim",
        narrative_structure="problem_solution",
        beats=[speech],
    )

    monkeypatch.setattr(P, "GeminiClient",
                        lambda *a, **k: _RecGemini(calls, timeline))
    monkeypatch.setattr(P, "make_voice_client", lambda s, provider=None: _FakeVoice())
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_image_client",
                        lambda s, u: _FakeAnchorClient(calls))
    monkeypatch.setattr(P, "make_video_client", lambda s, vg=None: _FakeVideoClient())
    monkeypatch.setattr(P, "make_lipsync_client", lambda s, e=None: object())
    monkeypatch.setattr(P, "voice_for", lambda g, v: "voice-x")
    monkeypatch.setattr(P, "beat_windows",
                        lambda beats, al, fs: [(0.0, 2.0)])
    monkeypatch.setattr(P, "split_long_windows",
                        lambda beats, w, al: (list(beats), list(w)))
    monkeypatch.setattr(P, "merge_short_windows",
                        lambda beats, w: (list(beats), list(w)))
    monkeypatch.setattr(P, "apply_holds",
                        lambda beats, windows, al, vo, *, work_dir:
                        (vo, al, list(windows)))
    monkeypatch.setattr(P, "slice_audio",
                        lambda src, a, b, dest: Path(dest).write_bytes(b""))
    monkeypatch.setattr(P, "download",
                        lambda url, local: Path(local).write_bytes(b""))

    anchor_build_calls: list = []
    real_build = P.build_timbre_anchor

    def spy_build(vo, work, uploader, **kw):
        url = "https://s3/anchor.wav"
        anchor_build_calls.append(url)
        # Actually write the file so uploader.upload works.
        anchor_path = Path(work) / "timbre-anchor.wav"
        anchor_path.write_bytes(b"")
        return "https://cdn/timbre-anchor.wav"
    monkeypatch.setattr(P, "build_timbre_anchor", spy_build)

    async def _fake_assemble(**kwargs):
        return SimpleNamespace(path=str(tmp_path / "out.mp4"),
                               duration_s=2.0, n_lipsynced=0)
    monkeypatch.setattr(P, "assemble", _fake_assemble)

    settings = Settings(
        gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
    )
    await P.generate_ugc_video(
        brief="sell the thing", settings=settings, work_dir=tmp_path / "work",
        out_path=tmp_path / "out.mp4", lipsync_engine="seedance_native",
        llm_provider="gemini", trim_silence=False,
        on_progress=lambda _m: None,
    )

    # Anchor was built exactly once.
    assert len(anchor_build_calls) == 1

    # The URL is in the manifest.
    import json as _json
    manifest = _json.loads((tmp_path / "work" / "manifest.json").read_text())
    assert manifest["timbre_anchor_url"] == "https://cdn/timbre-anchor.wav"


# ---------------------------------------------------------------------------
# heygen_replicate engine — unit tests
# ---------------------------------------------------------------------------


def test_heygen_replicate_in_lipsync_engines():
    """heygen_replicate must be an accepted engine string (fails fast before
    any paid call if missing)."""
    from app.pipeline import _LIPSYNC_ENGINES
    assert "heygen_replicate" in _LIPSYNC_ENGINES


@pytest.mark.asyncio
async def test_generate_ugc_video_accepts_heygen_replicate_engine(tmp_path):
    """heygen_replicate must not be rejected as an unknown engine."""
    from app.pipeline import generate_ugc_video
    # We pass a bad settings to prove it gets past the engine guard (the
    # first real failure comes later when it tries to build clients).
    try:
        await generate_ugc_video(
            brief="b", settings=None, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", lipsync_engine="heygen_replicate",
        )
    except ValueError as exc:
        if "lipsync_engine" in str(exc):
            pytest.fail(f"heygen_replicate rejected as unknown engine: {exc}")
    except Exception:
        pass   # expected — settings=None causes a downstream error


def test_beat_mode_heygen_replicate_maps_voice_mode():
    """heygen_replicate must route via resolve_voice_mode, not shot_type:
    a talking_head speaking beat → on_camera; a broll with dialogue →
    background_vo; a broll with no dialogue → silent; reaction → reaction.
    """
    from app.pipeline import _beat_mode
    assert _beat_mode(
        _tbeat(shot_type="talking_head", dialogue_text="hello"),
        lipsync_engine="heygen_replicate",
    ) == "on_camera"
    assert _beat_mode(
        _tbeat(shot_type="broll", framing=None, dialogue_text="absorbs fast"),
        lipsync_engine="heygen_replicate",
    ) == "background_vo"
    assert _beat_mode(
        _tbeat(shot_type="broll", framing=None, dialogue_text=""),
        lipsync_engine="heygen_replicate",
    ) == "silent"
    assert _beat_mode(
        _tbeat(shot_type="talking_head", beat_kind="reaction", dialogue_text=""),
        lipsync_engine="heygen_replicate",
    ) == "reaction"


def test_fallback_prompt_heygen_replicate_on_camera_uses_talking_prompt():
    """on_camera under heygen_replicate must use _talking_motion_prompt (the
    mouth-moving prompt for post-hoc lipsync), NOT the native timbre builder
    that embeds the dialogue line and 'Audio 1' reference."""
    from app.pipeline import _fallback_prompt, _talking_motion_prompt
    b = _tbeat(shot_type="talking_head", dialogue_text="this changed everything")
    p = _fallback_prompt(b, 4.0, lipsync_engine="heygen_replicate")
    expected = _talking_motion_prompt(b, 4.0)
    assert p == expected
    # Must NOT contain native-timbre tokens.
    assert "Audio 1" not in p
    assert "says exactly" not in p
    # Must contain the lipsync-prep (gentle) mouth clause.
    assert "subtle natural mouth movement" in p


def test_fallback_prompt_heygen_replicate_background_vo_uses_broll():
    """background_vo and silent under heygen_replicate both use _broll_motion_prompt
    (silent i2v, audio applied in assembly) — not native voice builders."""
    from app.pipeline import _fallback_prompt, _broll_motion_prompt
    bv = _tbeat(shot_type="broll", framing=None, dialogue_text="absorbs fast",
                broll_type="lifestyle", broll_shot="she pours", visual_note="pours")
    p_bv = _fallback_prompt(bv, 3.0, lipsync_engine="heygen_replicate")
    assert p_bv == _broll_motion_prompt("she pours", "pours", "lifestyle")
    # No native-timbre tokens.
    assert "Audio 1" not in p_bv

    sil = _tbeat(shot_type="broll", framing=None, dialogue_text="",
                 broll_type="lifestyle", broll_shot="she pours", visual_note="pours")
    p_sil = _fallback_prompt(sil, 3.0, lipsync_engine="heygen_replicate")
    assert p_sil == _broll_motion_prompt("she pours", "pours", "lifestyle")


def test_beat_mode_syncso_isolated_maps_voice_mode():
    """syncso in per_clip granularity uses isolated per-beat audio → beats
    route via resolve_voice_mode (on_camera/background_vo/silent), exactly
    like heygen_replicate — not the legacy sync_talk/broll routing. The
    legacy routing survives only when isolated_audio is False (syncso
    'whole' granularity / direct heygen engine)."""
    from app.pipeline import _beat_mode
    b = _tbeat(shot_type="talking_head", dialogue_text="hello")
    assert _beat_mode(b, lipsync_engine="syncso",
                      isolated_audio=True) == "on_camera"
    bb = _tbeat(shot_type="broll", framing=None, dialogue_text="")
    assert _beat_mode(bb, lipsync_engine="syncso",
                      isolated_audio=True) == "silent"
    bv = _tbeat(shot_type="broll", framing=None, dialogue_text="absorbs fast")
    assert _beat_mode(bv, lipsync_engine="syncso",
                      isolated_audio=True) == "background_vo"
    # legacy routing (whole granularity / direct heygen) unchanged:
    assert _beat_mode(b, lipsync_engine="syncso") == "sync_talk"
    assert _beat_mode(bb, lipsync_engine="syncso") == "broll"


def test_fallback_prompt_syncso_isolated_uses_silent_i2v_builders():
    """Isolated syncso clips are silent i2v: on_camera → talking-motion
    prompt (mouth-moving clause for post-hoc sync), background_vo/silent →
    b-roll prompt. Never the native timbre builders (those embed the
    dialogue line and 'Audio 1' reference)."""
    from app.pipeline import (_broll_motion_prompt, _fallback_prompt,
                              _talking_motion_prompt)
    b = _tbeat(shot_type="talking_head", dialogue_text="this changed everything")
    p = _fallback_prompt(b, 4.0, lipsync_engine="syncso", isolated_audio=True)
    assert p == _talking_motion_prompt(b, 4.0)
    assert "Audio 1" not in p

    bv = _tbeat(shot_type="broll", framing=None, dialogue_text="absorbs fast",
                broll_type="lifestyle", broll_shot="she pours", visual_note="pours")
    p_bv = _fallback_prompt(bv, 3.0, lipsync_engine="syncso", isolated_audio=True)
    assert p_bv == _broll_motion_prompt("she pours", "pours", "lifestyle")
    assert "Audio 1" not in p_bv


@pytest.mark.asyncio
async def test_write_motion_prompts_heygen_replicate_allows_opus_for_on_camera():
    """Under heygen_replicate, on_camera beats are NOT in native_timbre mode —
    Opus may be called (the prompt is a normal talking prompt, not a
    timbre-token-sensitive one). Source must be 'opus' when Opus succeeds."""
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt="OPUS-REPLICATE", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beat = _tbeat(index=0, shot_type="talking_head",
                  dialogue_text="this changed everything")
    char = SimpleNamespace(voice_persona=SimpleNamespace(
        gender="female", age_band="mid-20s", timbre="warm bright",
        pace="calm", warmth="friendly",
    ))
    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u0")], windows=[(0.0, 4.0)],
        gem=gem, character=char, llm="claude", product_ref_url=None,
        lipsync_engine="heygen_replicate", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert recs[0]["source"] == "opus"
    assert recs[0]["prompt"].startswith("OPUS-REPLICATE")


@pytest.mark.asyncio
async def test_generate_ugc_video_heygen_replicate_per_beat_audio(
    tmp_path, monkeypatch,
):
    """In heygen_replicate mode the pipeline must call generate_vo once PER
    speaking beat (not once globally), using the beat's dialogue_text and the
    resolved voice_id. Silent beats (no dialogue) must NOT get a TTS call.
    The beat_audio_paths / beat_audio_durs must be populated in the manifest.
    """
    import app.pipeline as P
    from app.config import Settings

    calls: list = []

    speech0 = _beat(index=0, dialogue_text="hello there friend",
                    shot_type="talking_head")
    speech1 = _beat(index=1, dialogue_text="and it works great",
                    shot_type="talking_head")
    silent_beat = _beat(index=2, dialogue_text="",
                        shot_type="broll", framing=None, broll_type="lifestyle")
    timeline = _Rec(
        full_script="hello there friend and it works great",
        target_duration_s=None, script_source="generated",
        hook_type="bold_claim", narrative_structure="problem_solution",
        beats=[speech0, speech1, silent_beat],
    )

    vo_call_args: list = []

    class _FakeVoiceSpy:
        """Tracks every generate_vo call; speaking beats get a short result."""
        async def generate_vo(self, *, script, voice_id, out_path,
                              voice_vibe=None):
            from app.voice import Alignment, VoiceResult
            vo_call_args.append({"script": script, "voice_id": voice_id,
                                 "voice_vibe": voice_vibe})
            Path(out_path).write_bytes(b"")
            n = max(1, len(script))
            align = Alignment(
                characters=list(script.ljust(n)[:n]),
                starts=[float(i) * 0.1 for i in range(n)],
                ends=[float(i + 1) * 0.1 for i in range(n)],
            )
            return VoiceResult(audio_path=str(out_path), alignment=align)

    monkeypatch.setattr(P, "GeminiClient",
                        lambda *a, **k: _RecGemini(calls, timeline))
    monkeypatch.setattr(P, "make_voice_client", lambda s, provider=None: _FakeVoiceSpy())
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_image_client",
                        lambda s, u: _FakeAnchorClient(calls))
    monkeypatch.setattr(P, "make_video_client", lambda s, vg=None: _FakeVideoClient())
    monkeypatch.setattr(P, "make_lipsync_client", lambda s, e=None: object())
    monkeypatch.setattr(P, "voice_for", lambda g, v: "voice-x")
    monkeypatch.setattr(P, "beat_windows",
                        lambda beats, al, fs: [(0.0, 1.8), (1.8, 3.6), (3.6, 4.0)])
    monkeypatch.setattr(P, "split_long_windows",
                        lambda beats, w, al: (list(beats), list(w)))
    monkeypatch.setattr(P, "apply_holds",
                        lambda beats, windows, al, vo, *, work_dir:
                        (vo, al, list(windows)))
    monkeypatch.setattr(P, "download",
                        lambda url, local: Path(local).write_bytes(b""))

    async def _fake_assemble(**kwargs):
        return SimpleNamespace(path=str(tmp_path / "out.mp4"),
                               duration_s=4.0, n_lipsynced=2)
    monkeypatch.setattr(P, "assemble", _fake_assemble)

    settings = Settings(
        gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
        replicate_api_token="x",
    )
    await P.generate_ugc_video(
        brief="sell the thing", settings=settings, work_dir=tmp_path / "work",
        out_path=tmp_path / "out.mp4", lipsync_engine="heygen_replicate",
        llm_provider="gemini", trim_silence=False,
        on_progress=lambda _m: None,
    )

    # The initial full-script VO call + 2 per-beat calls (one per speaking beat).
    # The silent_beat has no dialogue → no TTS call.
    per_beat_scripts = {c["script"] for c in vo_call_args}
    assert "hello there friend" in per_beat_scripts
    assert "and it works great" in per_beat_scripts
    # Silent beat must NOT have a TTS call.
    assert "" not in per_beat_scripts

    # The manifest must record the beat_audio_paths list.
    import json as _json
    manifest = _json.loads((tmp_path / "work" / "manifest.json").read_text())
    assert "beat_audio_paths" in manifest
    # Two speaking beats get paths; the silent one gets null.
    paths = manifest["beat_audio_paths"]
    assert paths[0] is not None    # speech0
    assert paths[1] is not None    # speech1
    assert paths[2] is None        # silent_beat


@pytest.mark.asyncio
async def test_generate_ugc_video_heygen_replicate_passes_replicate_flag(
    tmp_path, monkeypatch,
):
    """In heygen_replicate mode, assemble() must be called with
    isolated_audio=True, native_lipsync=False, and the beat_audio_paths /
    beat_audio_durs lists (not None)."""
    import app.pipeline as P
    from app.config import Settings

    calls: list = []
    speech = _beat(index=0, dialogue_text="hello there friend",
                   shot_type="talking_head")
    timeline = _Rec(
        full_script="hello there friend", target_duration_s=None,
        script_source="generated", hook_type="bold_claim",
        narrative_structure="problem_solution",
        beats=[speech],
    )

    monkeypatch.setattr(P, "GeminiClient",
                        lambda *a, **k: _RecGemini(calls, timeline))
    monkeypatch.setattr(P, "make_voice_client", lambda s, provider=None: _FakeVoice())
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_image_client",
                        lambda s, u: _FakeAnchorClient(calls))
    monkeypatch.setattr(P, "make_video_client", lambda s, vg=None: _FakeVideoClient())
    monkeypatch.setattr(P, "make_lipsync_client", lambda s, e=None: object())
    monkeypatch.setattr(P, "voice_for", lambda g, v: "voice-x")
    monkeypatch.setattr(P, "beat_windows",
                        lambda beats, al, fs: [(0.0, 2.0)])
    monkeypatch.setattr(P, "split_long_windows",
                        lambda beats, w, al: (list(beats), list(w)))
    monkeypatch.setattr(P, "apply_holds",
                        lambda beats, windows, al, vo, *, work_dir:
                        (vo, al, list(windows)))
    monkeypatch.setattr(P, "download",
                        lambda url, local: Path(local).write_bytes(b""))

    assemble_kwargs: dict = {}

    async def spy_assemble(**kwargs):
        assemble_kwargs.update(kwargs)
        return SimpleNamespace(path=str(tmp_path / "out.mp4"),
                               duration_s=2.0, n_lipsynced=1)
    monkeypatch.setattr(P, "assemble", spy_assemble)

    settings = Settings(
        gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
        replicate_api_token="x",
    )
    await P.generate_ugc_video(
        brief="sell the thing", settings=settings, work_dir=tmp_path / "work",
        out_path=tmp_path / "out.mp4", lipsync_engine="heygen_replicate",
        llm_provider="gemini", trim_silence=False,
        on_progress=lambda _m: None,
    )

    assert assemble_kwargs.get("isolated_audio") is True
    assert assemble_kwargs.get("native_lipsync") is False
    assert assemble_kwargs.get("beat_audio_paths") is not None
    assert assemble_kwargs.get("beat_audio_durs") is not None


# --- Product size/label stability under i2v motion (2026-07-22) -------------

def test_product_stability_clause_locks_size_and_label_adhesion():
    """Regression: product size drifted and a transparent-box label floated off
    the surface. The stability clause must lock SIZE/scale and keep printed text
    ADHERED to the surface, explicitly covering transparent packaging."""
    from app.pipeline import _PRODUCT_STABILITY_CLAUSE
    low = _PRODUCT_STABILITY_CLAUSE.lower()
    assert "size" in low and "rescale" in low                # scale lock
    assert "printed flat" in low                              # label adhered
    assert any(w in low for w in ("transparent", "see-through", "glass"))
    assert any(w in low for w in ("hovering", "detaching", "floating"))


@pytest.mark.asyncio
async def test_write_motion_prompts_force_appends_product_stability(monkeypatch):
    """A product beat whose Opus prompt omits the product-lock core must get
    the stability clause appended verbatim (never left to paraphrase) — same
    guarantee the anatomy clause already has."""
    from app.pipeline import _write_motion_prompts, _PRODUCT_STABILITY_CLAUSE
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        # Opus returns a prompt with NO product-lock vocabulary.
        return MotionPrompt(prompt=f"OPUS-{kw['beat'].index} slow push-in",
                            restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beats = [_tbeat(index=0, shot_type="broll", framing=None,
                    broll_type="product_closeup", broll_shot="the product")]
    recs = await _write_motion_prompts(
        beats=beats, anchors=[_anchor("u0")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="claude", product_ref_url="http://p.jpg",
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert "printed flat" in recs[0]["prompt"].lower()   # clause force-appended
    assert recs[0]["source"] == "opus"


@pytest.mark.asyncio
async def test_write_motion_prompts_no_product_stability_on_nonproduct(monkeypatch):
    """A non-product b-roll beat must NOT get the product clause appended."""
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt=f"OPUS-{kw['beat'].index} handheld", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    beats = [_tbeat(index=0, shot_type="broll", framing=None,
                    broll_type="lifestyle", broll_shot="a scene")]
    recs = await _write_motion_prompts(
        beats=beats, anchors=[_anchor("u0")], windows=[(0.0, 4.0)], gem=gem,
        character=object(), llm="claude", product_ref_url="http://p.jpg",
        lipsync_engine="syncso", on_progress=lambda _m: None,
        manifest=_FakeManifest(), on_asset=None,
    )
    assert "printed flat" not in recs[0]["prompt"].lower()


# --- Kling v3 b-roll provider (2026-07-25) ----------------------------------
# The feature is UI-selectable and DEFAULT OFF, so the tests split in two:
# routing/prompt-shape correctness when it IS on, and a hard regression lock
# proving a default run orders exactly the Seedance requests it always did.

def _kling_beat(**ov):
    """A product/hands b-roll beat (the Kling-eligible shape)."""
    d = dict(shot_type="broll", framing=None, broll_type="product_closeup",
             broll_shot="close on the jar resting on the counter",
             dialogue_text="", visual_note="the jar sits in the light")
    d.update(ov)
    return _tbeat(**d)


@pytest.mark.parametrize("mode", ["broll", "reaction", "silent",
                                  "background_vo"])
def test_use_kling_routes_every_non_speaking_mode(mode):
    """The four modes whose clips are silent i2v are Kling's whole remit."""
    from app.pipeline import _use_kling
    assert _use_kling(mode, native=False, broll_provider="kling") is True


@pytest.mark.parametrize("mode", ["broll", "reaction", "silent",
                                  "background_vo", "sync_talk", "on_camera",
                                  "native_talk"])
def test_use_kling_seedance_provider_never_routes(mode):
    """DEFAULT-OFF: with the provider left at "seedance" nothing routes to
    Kling, whatever the mode — the feature has to be completely inert."""
    from app.pipeline import _use_kling
    assert _use_kling(mode, native=False, broll_provider="seedance") is False


@pytest.mark.parametrize("mode", ["sync_talk", "on_camera", "native_talk"])
def test_use_kling_never_routes_talking_modes(mode):
    """Talking beats must carry either baked-in native speech or a mouth that
    post-hoc lipsync can drive — Seedance owns both paths."""
    from app.pipeline import _use_kling
    assert _use_kling(mode, native=False, broll_provider="kling") is False


@pytest.mark.parametrize("mode", ["background_vo", "on_camera"])
def test_use_kling_native_beats_stay_on_seedance(mode):
    """THE ROUTING CRUX: under lipsync_engine="seedance_native" a background_vo
    beat is generated WITH audio on the r2v model. It is visually plain b-roll,
    but Kling has no audio-driven generation at all — routing it there would
    silently drop the voiceover, so native=True must veto."""
    from app.pipeline import _use_kling
    assert _use_kling(mode, native=True, broll_provider="kling") is False


def test_clip_request_kling_routes_to_the_kling_model_with_its_negative():
    """A Kling-routed beat swaps the model + carries negative_prompt; duration
    is untouched (Kling's range is 3-15s, so the 4s Seedance floor is valid)."""
    from app.config import KLING_BROLL_MODEL
    req = _clip_request(
        _kling_beat(), anchor_url="https://a/x.png", duration=4, seed=7,
        native=False, prompt="motion.", audio_url=None, use_kling=True,
        negative_prompt="morphing product, warping label",
    )
    assert req.model == KLING_BROLL_MODEL
    assert req.negative_prompt == "morphing product, warping label"
    assert req.duration == 4
    assert req.first_frame_url == "https://a/x.png"


def test_clip_request_kling_drops_every_seedance_only_field():
    """Kling i2v has no seed (non-reproducible), no camera_fixed (the lock is
    prompt-side) and no audio-driven mode — the request must not carry them
    even though the shared VideoRequest dataclass has the fields."""
    req = _clip_request(
        _kling_beat(framing="tripod"), anchor_url="https://a/x.png",
        duration=4, seed=99, native=False, prompt="motion.", audio_url=None,
        use_kling=True, negative_prompt="blur",
    )
    assert req.seed is None
    assert req.camera_fixed is False
    assert req.generate_audio is False
    assert req.audio_url is None
    assert req.last_frame_url is None


def test_clip_request_seedance_path_carries_no_negative_prompt():
    """Regression: the default (Seedance) request must be exactly what it was
    before Kling existed — Seedance has no negative_prompt field at all."""
    req = _clip_request(
        _kling_beat(), anchor_url="https://a/x.png", duration=4, seed=7,
        native=False, prompt="motion.", audio_url=None,
    )
    from app.video_gen import DEFAULT_MODEL_ID
    assert req.model == DEFAULT_MODEL_ID
    assert req.negative_prompt is None
    assert req.seed == 7


# ---- the deterministic Kling prompt repair --------------------------------

def test_kling_positive_strips_camera_directions_on_a_locked_shot():
    """Kling has no camera_fixed: the ONLY lock is saying nothing about the
    camera in the positive (the moves are banned in negative_prompt), so a
    camera sentence would fight its own negative and must be dropped."""
    from app.pipeline import _KLING_CAMERA_RE, _kling_positive
    src = ("The camera makes one very slow, smooth, continuous push-in. "
           "She lifts the jar toward the lens. "
           "Filmed on an iPhone front camera, handheld with a slight sway.")
    out = _kling_positive(src, micro_motion="Her hands stay steady.")
    assert "She lifts the jar toward the lens." in out
    assert not _KLING_CAMERA_RE.search(out)
    # "lens" is SUBJECT direction in this codebase, not a camera move — the
    # repair must not treat it as camera vocabulary (doing so deleted the only
    # motion out of a product beat).
    assert "lens" in out


def test_kling_positive_moves_ban_vocabulary_out_of_the_positive():
    """Every affirmative ban clause exists only because Seedance lacks
    negative_prompt. Kling HAS the field, so bans must not burn the positive's
    char budget (and must not re-describe the product, which is what triggers
    Kling's documented camera-cut failure)."""
    from app.pipeline import _kling_positive
    src = ("She turns the jar a few degrees. "
           "The label never warps, stretches, or duplicates. "
           "No extra hands enter the frame.")
    out = _kling_positive(src, micro_motion="")
    assert out.startswith("She turns the jar a few degrees.")
    assert "never" not in out.lower()
    assert "no extra hands" not in out.lower()


def test_kling_positive_fits_the_char_cap_on_a_sentence_boundary():
    from app.pipeline import _KLING_PROMPT_CHARS, _kling_positive
    src = " ".join(
        [f"She moves the jar a little in step {i}." for i in range(30)])
    out = _kling_positive(src, micro_motion="Her hands stay steady.")
    assert len(out) <= _KLING_PROMPT_CHARS
    # Cut on a boundary, not mid-clause: a dangling fragment is something Kling
    # would have to interpret.
    assert "Her hands stay steady." in out
    assert out.count("step") >= 1


def test_kling_positive_hard_cuts_a_single_over_long_run_on():
    """An Opus run-on with no internal sentence boundary still has to fit —
    cut on the last WORD boundary and closed, never mid-word."""
    from app.pipeline import _KLING_PROMPT_CHARS, _kling_positive
    src = " ".join(["she slowly turns the jar while the light moves"] * 12) + "."
    assert len(src) > _KLING_PROMPT_CHARS
    out = _kling_positive(src, micro_motion="Her hands stay steady.")
    assert len(out) <= _KLING_PROMPT_CHARS
    assert not out.split(". ")[0].endswith(("th", "mov", "ligh"))   # no mid-word


def test_kling_positive_appends_the_preserve_tail_exactly_once():
    """The faithfulness tail must land once even when the source already
    carried it (Opus is handed the Seedance prompt as its example)."""
    from app.pipeline import _FAITHFULNESS_CLAUSE, _kling_positive
    src = f"She turns the jar slowly. {_FAITHFULNESS_CLAUSE}"
    out = _kling_positive(src, micro_motion="")
    assert out.count(_FAITHFULNESS_CLAUSE) == 1
    assert out.endswith(_FAITHFULNESS_CLAUSE)


def test_kling_positive_never_ships_a_tail_only_prompt():
    """Nothing survivable in → empty out. Shipping the faithfulness tail alone
    would be a prompt with zero stated motion, i.e. Kling's frozen clip."""
    from app.pipeline import _kling_positive
    assert _kling_positive("", micro_motion="") == ""
    assert _kling_positive("The product never warps.", micro_motion="") == ""


def test_kling_broll_prompt_shape_from_a_real_seedance_source():
    """End-to-end repair of a genuine _broll_motion_prompt (what Opus is shown
    and may echo): ≤400 chars, no camera language, one tail, one named motion."""
    from app.pipeline import (_FAITHFULNESS_CLAUSE, _KLING_CAMERA_RE,
                              _KLING_PROMPT_CHARS, _kling_broll_prompt)
    beat = _kling_beat()
    source = _broll_motion_prompt(
        beat.broll_shot, beat.visual_note, beat.broll_type)
    assert len(source) > 1000      # the Seedance form really is 180-250 words
    positive, negative = _kling_broll_prompt(
        beat, mode="broll", motion_source=source)
    assert len(positive) <= _KLING_PROMPT_CHARS
    assert positive.count(_FAITHFULNESS_CLAUSE) == 1
    assert positive.endswith(_FAITHFULNESS_CLAUSE)
    assert not _KLING_CAMERA_RE.search(positive)
    assert negative                # a Kling beat always ships a negative


def test_kling_broll_prompt_falls_back_to_the_shot_line():
    """Without Opus (llm != claude) there is no motion source; the planner's
    shot line is the subject, per Kling's "Subject + Movement" formula."""
    from app.pipeline import _kling_broll_prompt
    beat = _kling_beat(broll_shot="macro of the dropper with a drop forming")
    positive, _ = _kling_broll_prompt(beat, mode="broll", motion_source=None)
    assert positive.startswith("Macro of the dropper with a drop forming.")
    # visual_note is deliberately absent: it re-describes staging the anchor
    # image already encodes, and re-description is what makes Kling cut.
    assert beat.visual_note not in positive


def test_kling_reaction_prompt_names_a_motion_instead_of_settling():
    """Kling warns a vague prompt comes back FROZEN, and _SETTLE_CLAUSE is a
    literal instruction to stop moving — the reaction path must replace it with
    a named micro-motion. Reaction beats have no shot line and need none."""
    from app.pipeline import (_KLING_REACTION_MOTION, _SETTLE_CLAUSE,
                              _kling_broll_prompt)
    beat = _kling_beat(beat_kind="reaction", broll_type="lifestyle",
                       broll_shot=None, shows_creator=True)
    positive, negative = _kling_broll_prompt(beat, mode="reaction",
                                             motion_source=None)
    assert _SETTLE_CLAUSE not in positive
    # The named reaction motion IS the prompt (sentence-cased by the builder).
    assert _KLING_REACTION_MOTION.split(",")[0].lower() in positive.lower()
    assert "frozen, static" in negative      # the dead-clip ban


def test_kling_broll_positive_never_carries_the_settle_clause():
    """The same dead-clip rule as the reaction path, on the B-ROLL path.

    _SETTLE_CLAUSE is affirmative, so it carries no _KLING_BAN_RE word, and
    "the shot settles" carries no _KLING_CAMERA_RE word — it used to sail
    through the repair untouched on lifestyle b-roll, leaving the positive
    telling Kling to hold a static framing AND to keep moving (the appended
    micro-motion) in one prompt, on the one provider whose documented failure
    is a frozen clip.
    """
    from app.pipeline import (_SETTLE_CLAUSE, _kling_broll_prompt,
                              _kling_micro_motion)
    beat = _kling_beat(broll_type="lifestyle", shows_creator=True,
                       broll_shot="she walks through the kitchen",
                       visual_note="she smiles")
    source = _broll_motion_prompt(
        beat.broll_shot, beat.visual_note, beat.broll_type)
    assert _SETTLE_CLAUSE in source            # the Seedance form really has it
    positive, _ = _kling_broll_prompt(beat, mode="broll", motion_source=source)
    assert _SETTLE_CLAUSE not in positive
    assert "settles" not in positive.lower()
    # ...and the one thing that MUST survive is the named motion.
    assert _kling_micro_motion(beat, mode="broll") in positive


@pytest.mark.parametrize("paraphrase", [
    "The action resolves early, then the shot holds a static framing.",
    "She finishes the movement and the frame settles for the last second.",
    "Her gesture completes, then everything comes to a rest.",
    "The motion stops moving before the clip ends.",
])
def test_kling_positive_drops_hold_still_paraphrases(paraphrase):
    """The clause reaches Kling via OPUS (it rides in as fallback_example), so
    a substring match on _SETTLE_CLAUSE is not enough — the repair has to catch
    the paraphrase Opus actually writes."""
    from app.pipeline import _kling_positive
    out = _kling_positive(f"She walks to the counter. {paraphrase}",
                          micro_motion="She keeps moving gently.")
    assert "She walks to the counter." in out
    assert paraphrase not in out


def test_kling_fit_returns_nothing_on_a_non_positive_budget():
    """`budget` is used as a slice index, and s[:-10] silently returns a LONG
    fragment (everything but the last 10 chars) instead of "". Reachable only if
    the 400-char cap is lowered or a micro-motion phrase grows past ~340 chars,
    but a paid prompt is the wrong place to discover that."""
    from app.pipeline import _kling_fit
    assert _kling_fit(["a" * 80], -10) == ""
    assert _kling_fit(["a" * 80], 0) == ""


@pytest.mark.parametrize("broll_type", ["product_closeup", "product_in_scene",
                                        "hands_using", "unboxing",
                                        "texture_pour", "demo_steps",
                                        "before_after"])
def test_kling_negative_picks_the_product_list_for_product_and_hands(broll_type):
    from app.pipeline import _KLING_NEG_PRODUCT, _kling_negative
    assert _kling_negative(_kling_beat(broll_type=broll_type),
                           mode="broll") == _KLING_NEG_PRODUCT


def test_kling_negative_picks_the_person_list_for_lifestyle_and_reaction():
    """Lifestyle b-roll and reaction cutaways have a person in frame, not a
    label to protect — they need the anatomy/skin bans instead."""
    from app.pipeline import _KLING_NEG_PERSON, _kling_negative
    lifestyle = _kling_beat(broll_type="lifestyle", shows_creator=True)
    assert _kling_negative(lifestyle, mode="broll") == _KLING_NEG_PERSON
    # A reaction beat is a person shot even when broll_type says product.
    assert _kling_negative(_kling_beat(beat_kind="reaction"),
                           mode="reaction") == _KLING_NEG_PERSON


def test_kling_negatives_are_bare_noun_phrases_not_no_x_phrasing():
    """Kling's negative_prompt takes artifact NAMES; "no X" phrasing there is
    a known no-op (and on some models actively re-introduces X)."""
    from app.pipeline import _KLING_NEG_PERSON, _KLING_NEG_PRODUCT
    for neg in (_KLING_NEG_PRODUCT, _KLING_NEG_PERSON):
        terms = [t.strip() for t in neg.split(",")]
        assert all(terms)
        assert not any(t.startswith(("no ", "never ", "not ", "without "))
                       for t in terms), terms
        for banned in ("scene cut", "transition", "camera shake", "blur",
                       "low quality", "watermark"):
            assert banned in terms


def test_kling_person_negatives_keep_skin_notes_flattering():
    """Project rule: skin-texture defense bans the plastic/waxy CGI sheen and
    blotchiness — it must NEVER ask for pores/oil emphasis that makes an
    attractive creator read greasy."""
    from app.pipeline import _KLING_NEG_PERSON
    for banned_artifact in ("plastic skin", "waxy skin", "oily skin",
                            "blotchy skin"):
        assert banned_artifact in _KLING_NEG_PERSON
    assert "pore" not in _KLING_NEG_PERSON      # never negate real texture


def test_kling_micro_motions_carry_no_camera_or_ban_vocabulary():
    """The named micro-motion is the beat's guaranteed movement, appended AFTER
    the repair — if it contained camera/ban words the repair could not have
    stripped them, and the char-budget reservation would leak them through."""
    from app.pipeline import (_KLING_BAN_RE, _KLING_CAMERA_RE,
                              _KLING_MICRO_MOTION, _KLING_MICRO_MOTION_DEFAULT,
                              _KLING_REACTION_MOTION)
    phrases = list(_KLING_MICRO_MOTION.values()) + [
        _KLING_MICRO_MOTION_DEFAULT, _KLING_REACTION_MOTION]
    for phrase in phrases:
        assert not _KLING_CAMERA_RE.search(phrase), phrase
        assert not _KLING_BAN_RE.search(phrase), phrase


# ---- stage 5b provider routing --------------------------------------------

@pytest.mark.asyncio
async def test_write_motion_prompts_kling_records_provider_and_negative():
    """Provider routing is decided ONCE, in stage 5b, and persisted per beat —
    the clip stage (and a resume) reads it back instead of re-deriving it."""
    from app.pipeline import _KLING_PROMPT_CHARS, _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt="OPUS: she turns the jar slowly.",
                            restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    talking = _tbeat(index=0, shot_type="talking_head")
    broll = _kling_beat(index=1)
    recs = await _write_motion_prompts(
        beats=[talking, broll], anchors=[_anchor("u0"), _anchor("u1")],
        windows=[(0.0, 3.0), (3.0, 6.0)], gem=gem, character=object(),
        llm="claude", product_ref_url="http://p.jpg",
        lipsync_engine="syncso", broll_video_provider="kling",
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    # Talking head stays on Seedance with no negative.
    assert recs[0]["provider"] == "seedance"
    assert recs[0]["negative_prompt"] is None
    # B-roll flipped, with a Kling-shaped prompt + its negative.
    assert recs[1]["provider"] == "kling"
    assert recs[1]["negative_prompt"]
    assert len(recs[1]["prompt"]) <= _KLING_PROMPT_CHARS


@pytest.mark.asyncio
async def test_write_motion_prompts_kling_skips_the_seedance_force_appends():
    """The anatomy + product-stability clauses are force-appended on Seedance
    ONLY because Seedance has no negative_prompt. On Kling those bans ride in
    negative_prompt; leaving them in the positive would burn the char budget
    AND re-describe the product (Kling's camera-cut trigger)."""
    from app.pipeline import _KLING_NEG_PRODUCT, _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        # No anti-warp core and no product-lock core → both Seedance
        # force-appends would fire.
        return MotionPrompt(prompt="She turns the jar slowly.", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    recs = await _write_motion_prompts(
        beats=[_kling_beat(index=0)], anchors=[_anchor("u0")],
        windows=[(0.0, 4.0)], gem=gem, character=object(),
        llm="claude", product_ref_url="http://p.jpg",
        lipsync_engine="syncso", broll_video_provider="kling",
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    prompt = recs[0]["prompt"]
    assert "never stretched" not in prompt            # anatomy guard skipped
    assert "printed flat" not in prompt.lower()       # product lock skipped
    assert recs[0]["negative_prompt"] == _KLING_NEG_PRODUCT
    assert "warped hands" in recs[0]["negative_prompt"]   # the guard, relocated


@pytest.mark.asyncio
async def test_write_motion_prompts_default_provider_leaves_prompts_untouched():
    """DEFAULT-OFF regression at the prompt stage: with the provider left at
    its default the record is byte-identical to the pre-Kling behaviour."""
    from app.pipeline import _write_motion_prompts
    from app.gemini import MotionPrompt

    async def fake_write(**kw):
        return MotionPrompt(prompt="She turns the jar slowly.", restaged=False)
    gem = SimpleNamespace(write_motion_prompt=fake_write)

    async def _run(**extra):
        return await _write_motion_prompts(
            beats=[_kling_beat(index=0)], anchors=[_anchor("u0")],
            windows=[(0.0, 4.0)], gem=gem,
            character=object(), llm="claude", product_ref_url="http://p.jpg",
            lipsync_engine="syncso", on_progress=lambda _m: None,
            manifest=_FakeManifest(), on_asset=None, **extra,
        )

    implicit = await _run()
    explicit = await _run(broll_video_provider="seedance")
    assert implicit[0] == explicit[0]
    assert implicit[0]["provider"] == "seedance"
    assert implicit[0]["negative_prompt"] is None
    # The Seedance safety force-appends still fire on the default path.
    assert "never stretched" in implicit[0]["prompt"]
    assert "printed flat" in implicit[0]["prompt"].lower()


# ---- per-run cost estimate ------------------------------------------------

def test_clip_cost_estimate_prices_each_provider_on_ordered_seconds():
    """13.5 credits/s seedance vs 5.4 credits/s kling at $0.014722/credit —
    billed on the ORDERED duration, since the trim to the beat window happens
    locally long after EvoLink charged."""
    from app.pipeline import _clip_cost_estimate
    cost = _clip_cost_estimate({
        0: {"provider": "seedance", "duration_requested": 4},
        1: {"provider": "kling", "duration_requested": 4},
        2: {"duration_requested": 4},          # unknown → counted as seedance
    })
    assert cost["by_provider"]["seedance"]["clips"] == 2
    assert cost["by_provider"]["seedance"]["credits"] == pytest.approx(108.0)
    assert cost["by_provider"]["kling"]["credits"] == pytest.approx(21.6)
    assert cost["total_credits"] == pytest.approx(129.6)
    assert cost["total_usd"] == pytest.approx(129.6 * 0.014722)
    # ~2.5x cheaper per second is the whole point of the feature.
    assert (cost["by_provider"]["kling"]["credits"] * 2.5
            == pytest.approx(54.0))


# ---- full mocked run: the feature must be INERT by default -----------------

async def _run_v2(tmp_path, monkeypatch, *, work, **extra):
    """One fully-mocked generate_ugc_video run; returns (requests, manifest).

    Every paid client is faked (no EvoLink / ElevenLabs / Claude / Gemini call)
    and the VideoRequest objects handed to EvoLink are captured verbatim so the
    default-provider path can be diffed against the Kling one.
    """
    import app.pipeline as P
    from app.config import Settings

    requests: list = []
    calls: list = []

    talking = _beat(index=0, dialogue_text="hi", shot_type="talking_head")
    vo_broll = _beat(index=1, dialogue_text="ok", shot_type="broll",
                     framing=None, broll_type="lifestyle",
                     broll_shot="she walks through the kitchen",
                     shows_creator=True)
    silent_broll = _beat(index=2, dialogue_text="", shot_type="broll",
                         framing=None, broll_type="product_closeup",
                         broll_shot="close on the jar on the counter",
                         shows_creator=False)
    reaction = _beat(index=3, dialogue_text="", shot_type="broll",
                     framing=None, broll_type="lifestyle",
                     beat_kind="reaction", hold_s=0.6, shows_creator=True)
    beats = [talking, vo_broll, silent_broll, reaction]
    timeline = _Rec(full_script="hi ok", target_duration_s=None,
                    script_source="generated", hook_type="bold_claim",
                    narrative_structure="problem_solution", beats=beats)

    class _SpyVideoClient:
        async def create_task_async(self, request):
            requests.append(request)
            return f"task-{len(requests)}", {}

        async def poll_task_stream_async(self, task_id):
            yield "completed", SimpleNamespace(
                video_url=f"https://cdn/{task_id}.mp4")

    monkeypatch.setattr(P, "GeminiClient",
                        lambda *a, **k: _RecGemini(calls, timeline))
    monkeypatch.setattr(P, "make_voice_client", lambda s, provider=None: _FakeVoice())
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_image_client",
                        lambda s, u: _FakeAnchorClient(calls))
    monkeypatch.setattr(P, "make_video_client", lambda s, vg=None: _SpyVideoClient())
    monkeypatch.setattr(P, "make_lipsync_client", lambda s, e=None: object())
    monkeypatch.setattr(P, "voice_for", lambda g, v: "voice-x")
    windows = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0), (6.0, 6.6)]
    monkeypatch.setattr(P, "beat_windows", lambda b, al, fs: list(windows))
    monkeypatch.setattr(P, "split_long_windows",
                        lambda b, w, al: (list(b), list(w)))
    monkeypatch.setattr(P, "merge_short_windows",
                        lambda b, w: (list(b), list(w)))
    monkeypatch.setattr(P, "apply_holds",
                        lambda b, w, al, vo, *, work_dir: (vo, al, list(w)))
    monkeypatch.setattr(P, "download",
                        lambda url, local: Path(local).write_bytes(b"clip"))

    async def _fake_assemble(**kwargs):
        return SimpleNamespace(path=str(tmp_path / "out.mp4"),
                               duration_s=6.6, n_lipsynced=1)
    monkeypatch.setattr(P, "assemble", _fake_assemble)

    settings = Settings(
        gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
    )
    await P.generate_ugc_video(
        brief="sell the thing", settings=settings, work_dir=work,
        out_path=tmp_path / "out.mp4", llm_provider="gemini",
        trim_silence=False, visual_qc=False, on_progress=lambda _m: None,
        **extra,
    )
    import json as _json
    manifest = _json.loads((Path(work) / "manifest.json").read_text())
    return requests, manifest, beats, windows


@pytest.mark.asyncio
async def test_generate_ugc_video_default_provider_is_pure_seedance(
    tmp_path, monkeypatch,
):
    """THE default-off regression lock: an unconfigured run must order exactly
    the Seedance requests it ordered before Kling existed — same model, same
    per-beat deterministic seed, same camera lock, the same deterministic
    prompt, and no negative_prompt anywhere."""
    import app.pipeline as P
    from app.video_gen import DEFAULT_MODEL_ID

    work = tmp_path / "work"
    reqs, manifest, beats, windows = await _run_v2(
        tmp_path, monkeypatch, work=work)

    assert len(reqs) == 4
    by_prompt = {r.prompt: r for r in reqs}
    for i, (beat, window) in enumerate(zip(beats, windows)):
        expected = P._fallback_prompt(
            beat, window[1] - window[0], lipsync_engine="syncso",
            isolated_audio=True,
        )
        # syncso/per_clip is the default → isolated audio, silent i2v clips.
        assert expected in by_prompt, f"beat {i} prompt changed"
        req = by_prompt[expected]
        assert req.model == DEFAULT_MODEL_ID
        assert req.negative_prompt is None
        assert req.generate_audio is False
        assert req.audio_url is None
        assert req.camera_fixed is P._camera_fixed(beat)
        assert req.resolution == "720p"
        assert req.aspect_ratio == "9:16"
    # Seeds are the per-beat deterministic ones (unchanged addressing).
    assert sorted(r.seed for r in reqs) == sorted(
        P._seed_for(work, i) for i in range(4))

    # The manifest records the default and every clip as Seedance, and the
    # spend estimate has a single provider bucket.
    assert manifest["params"]["broll_video_provider"] == "seedance"
    assert {c["provider"] for c in manifest["clips"]} == {"seedance"}
    assert all(c["negative_prompt"] is None for c in manifest["clips"])
    assert set(manifest["clip_cost_estimate"]["by_provider"]) == {"seedance"}
    assert {r["provider"] for r in manifest["motion_prompts"]} == {"seedance"}


@pytest.mark.asyncio
async def test_generate_ugc_video_kling_flips_only_the_broll_requests(
    tmp_path, monkeypatch,
):
    """Turning Kling on must change the b-roll/reaction requests and NOTHING
    else: the talking head's request stays byte-identical (same frozen
    VideoRequest, seed included — both runs share the work dir, so the seeds
    are directly comparable)."""
    from app.config import KLING_BROLL_MODEL
    from app.video_gen import DEFAULT_MODEL_ID, provider_for_model

    work = tmp_path / "work"
    base_reqs, base_manifest, _, _ = await _run_v2(
        tmp_path, monkeypatch, work=work)
    kling_reqs, kling_manifest, _, _ = await _run_v2(
        tmp_path, monkeypatch, work=work, broll_video_provider="kling")

    base_talking = [r for r in base_reqs if r.model == DEFAULT_MODEL_ID
                    and "talks to the lens" in r.prompt]
    kling_talking = [r for r in kling_reqs if r.model == DEFAULT_MODEL_ID]
    assert len(base_talking) == 1 and len(kling_talking) == 1
    # Frozen dataclass equality: the talking-head order is untouched.
    assert kling_talking[0] == base_talking[0]

    flipped = [r for r in kling_reqs if r.model == KLING_BROLL_MODEL]
    assert len(flipped) == 3            # background_vo + silent + reaction
    for req in flipped:
        assert provider_for_model(req.model) == "kling"
        assert req.negative_prompt
        assert req.seed is None
        assert req.audio_url is None
        assert len(req.prompt) <= 400
    # Both providers appear in the spend estimate, and Kling is the cheap one.
    by_prov = kling_manifest["clip_cost_estimate"]["by_provider"]
    assert set(by_prov) == {"seedance", "kling"}
    assert (kling_manifest["clip_cost_estimate"]["total_usd"]
            < base_manifest["clip_cost_estimate"]["total_usd"])


@pytest.mark.asyncio
async def test_generate_ugc_video_rejects_unknown_broll_provider(tmp_path):
    """Fail fast BEFORE any paid call — same rule as the engine/LLM guards."""
    import app.pipeline as P
    from app.config import Settings
    settings = Settings(
        gemini_api_key="x", anthropic_api_key="x", elevenlabs_api_key="x",
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
    )
    with pytest.raises(ValueError, match="broll_video_provider"):
        await P.generate_ugc_video(
            brief="x", settings=settings, work_dir=tmp_path / "w",
            out_path=tmp_path / "o.mp4", broll_video_provider="veo",
            on_progress=lambda _m: None,
        )


def test_pipeline_entrypoint_exposes_broll_video_provider():
    """Every pipeline knob must be reachable (and defaulted) at the entrypoint
    so the UI can thread it — project rule (tasks/lessons.md:61)."""
    import inspect

    from app.config import BROLL_VIDEO_PROVIDER, BROLL_VIDEO_PROVIDERS
    from app.pipeline import generate_ugc_video
    params = inspect.signature(generate_ugc_video).parameters
    assert params["broll_video_provider"].default == BROLL_VIDEO_PROVIDER
    assert BROLL_VIDEO_PROVIDER == "seedance"          # DEFAULT OFF
    assert BROLL_VIDEO_PROVIDERS == ("seedance", "kling")


# ── Native vocal delivery direction (2026-07-29) ─────────────────────────────
# seedance_native has the VIDEO model synthesize the speech, so the ONLY prosody
# it gets is what the prompt says. The prompt used to carry identity + rate and a
# per-section tone describing the FACE — no vocal emotion direction at all, which
# is why the read was flat. The DIRECTOR now supplies it per line.

def test_native_prompt_carries_the_directors_voice_direction():
    from app.pipeline import _native_talking_prompt
    beat = _tbeat(dialogue_text="This actually worked.", visual_note="x")
    beat.voice_direction = ("In a delighted, surprised state, with a warm tone "
                            "and a lively, real-time pace")
    p = _native_talking_prompt(beat)
    assert "In a delighted, surprised state" in p
    assert "lively, real-time pace" in p


def test_native_prompt_falls_back_when_director_gave_no_direction():
    """Resumed runs / older manifests have no voice_direction. The fallback is
    ONE generic brisk line — deliberately not a per-section lookup table, which
    would give every hook the same read regardless of the words."""
    from app.pipeline import _VOICE_DIRECTION_FALLBACK, _native_talking_prompt
    beat = _tbeat(dialogue_text="This actually worked.", visual_note="x")
    beat.voice_direction = ""
    assert _VOICE_DIRECTION_FALLBACK in _native_talking_prompt(beat)


def test_native_spoken_line_respells_and_decaps():
    """Two transforms, both because Seedance IS the TTS here: respell the
    planner-flagged words (BytePlus's own documented fix for mispronunciation),
    and de-CAPS emphasis (case inside a dialogue string is UNDOCUMENTED, so
    ALL-CAPS is undefined behaviour and a plausible spelled-out reading)."""
    from app.pipeline import _native_spoken_line

    class _P:
        word, respelling = "Ayurvedic", "aa-yur-VAY-dik"

    beat = _tbeat(dialogue_text="[excited] This Ayurvedic oil is INSANE.",
                  visual_note="x")
    line = _native_spoken_line(beat, [_P()])
    assert "aa-yur-VAY-dik" in line
    assert "Ayurvedic" not in line
    assert "INSANE" not in line and "insane" in line
    assert "[excited]" not in line          # tags are never spoken


def test_native_spoken_line_leaves_short_acronyms_alone():
    from app.pipeline import _native_spoken_line
    beat = _tbeat(dialogue_text="Use SPF every day.", visual_note="x")
    assert "SPF" in _native_spoken_line(beat, [])


def test_section_voice_table_is_gone():
    """Delivery is the director's call, per line — a static ad_section table was
    removed on purpose. Guards against it creeping back."""
    import app.pipeline as P
    assert not hasattr(P, "_SECTION_VOICE")


# ── Omni end-to-end video generator mode (2026-07-29) ────────────────────────
# gemini-omni-flash-preview authors video AND voice in one call: no TTS, no
# lipsync pass, a synthetic word-budget clock instead of a VO master clock, and
# the whole run rides assemble()'s native_lipsync=True path (same contract as
# seedance_native). See docs/gemini-omni-research.md and the plan doc.

@pytest.mark.asyncio
async def test_generate_ugc_video_rejects_unknown_video_generator(tmp_path):
    """Fail fast before any client is built or paid call is made — settings=None
    proves nothing else was touched (same rule as every other engine guard)."""
    from app.config import VIDEO_GENERATORS
    from app.pipeline import generate_ugc_video
    with pytest.raises(ValueError, match="video_generator") as exc_info:
        await generate_ugc_video(
            brief="b", settings=None, work_dir=tmp_path,
            out_path=tmp_path / "o.mp4", video_generator="veo",
        )
    for g in VIDEO_GENERATORS:
        assert g in str(exc_info.value)


# --- _omni_windows: the synthetic word-budget clock -------------------------

def test_omni_windows_contiguous_and_stable_when_every_beat_clears_the_floor(
    monkeypatch,
):
    """Three beats that each clear OMNI_DUR_MIN: no split, no merge, windows
    are contiguous, and the silent/reaction beat's span is max(3, hold_s).

    Grouping off: this pins the SHOT builder. Folding beats into multi-shot takes
    is a separate stage with its own tests.
    """
    import app.pipeline as _p
    monkeypatch.setattr(_p, "OMNI_MULTISHOT", False)
    from app.pipeline import _omni_windows
    talk = _tbeat(index=0, shot_type="talking_head",
                  dialogue_text="one two three four five")
    vo = _tbeat(index=1, shot_type="broll", broll_type="lifestyle",
                dialogue_text="alpha beta gamma delta echo")
    silent = _tbeat(index=2, shot_type="broll", broll_type="product_closeup",
                    dialogue_text="", beat_kind="reaction", hold_s=4.0)

    beats, windows, n_split = _omni_windows([talk, vo, silent])

    assert n_split == 0
    assert len(beats) == 3
    assert windows[0][0] == 0.0
    # Contiguous: each window starts exactly where the previous one ended.
    for (_, e0), (s1, _) in zip(windows, windows[1:]):
        assert e0 == pytest.approx(s1)
    # Total planned duration is just the sum of the per-beat spans.
    total = sum(e - s for s, e in windows)
    assert windows[-1][1] == pytest.approx(total)
    # Silent/reaction beat: max(floor, hold) clamped to the shot ceiling.
    from app.config import OMNI_SHOT_MAX_S
    assert windows[2][1] - windows[2][0] == pytest.approx(
        min(4.0, OMNI_SHOT_MAX_S))


from app.config import OMNI_SHOT_MAX_S


@pytest.mark.parametrize("hold_s,expected", [
    (None, 3.0),   # no hold at all -> the 3s floor
    (0.0, 3.0),
    (1.0, 3.0),    # a hold shorter than the floor still floors to 3.0
    # A hold longer than the floor wins only up to the shot ceiling — a "hold on
    # the product" is still a shot, and a 6s one is the dead frame the fast-cut
    # bar exists to kill (2026-08-05).
    (6.0, OMNI_SHOT_MAX_S),
])
def test_omni_windows_silent_beat_span_is_max_of_floor_and_hold(hold_s, expected):
    from app.pipeline import _omni_windows
    beat = _tbeat(index=0, beat_kind="reaction", dialogue_text="", hold_s=hold_s)
    beats, windows, n_split = _omni_windows([beat])
    assert windows[0][1] - windows[0][0] == pytest.approx(expected)


def test_omni_windows_splits_overlong_beat_into_word_chunks(monkeypatch):
    """A beat past the SHOT ceiling is word-chunked into sub-beats whose
    dialogue_text chunks PARTITION the original words — no drops, no duplicates,
    order preserved — and each chunk gets its own sub_index.

    The bar is OMNI_SHOT_MAX_S/OMNI_SHOT_TARGET_S (the fast-cut bar the seedance
    path applies in split_long_windows), NOT the 10s API ceiling: splitting only
    above the API ceiling is what let a 20-word beat become one 10s shot that
    hangs on a single frame (2026-08-05).
    """
    from app.config import (OMNI_SHOT_MAX_S, OMNI_SHOT_TARGET_S,
                            OMNI_WORDS_PER_SECOND)
    import app.pipeline as _p
    monkeypatch.setattr(_p, "OMNI_MULTISHOT", False)
    from app.pipeline import _omni_windows
    words = [f"w{i}" for i in range(40)]
    long_beat = _tbeat(index=0, shot_type="talking_head",
                       dialogue_text=" ".join(words))

    beats, windows, n_split = _omni_windows([long_beat])

    # 40 words at OMNI_WORDS_PER_SECOND = 20s of speech, cut at ~3s a shot.
    assert n_split == len(beats) - 1
    assert len(beats) == math.ceil(
        (len(words) / OMNI_WORDS_PER_SECOND) / OMNI_SHOT_TARGET_S)
    rebuilt = " ".join(b.dialogue_text for b in beats).split()
    assert rebuilt == words              # exact partition of the original line
    for i, b in enumerate(beats):
        assert b.sub_index == i
        assert b.index == i
    # The whole point: no shot is left sitting past the fast-cut ceiling.
    for s, e in windows:
        assert 0 < (e - s) <= OMNI_SHOT_MAX_S


def test_omni_windows_floors_a_short_talking_beat_instead_of_merging_it_away(
    monkeypatch,
):
    """A talking window under OMNI_DUR_MIN is FLOORED to the minimum, never
    merged. merge_short_windows keeps only the dominant beat's fields, and in
    omni a beat's prompt is the ONLY place its line is spoken (no continuous VO
    underneath), so merging would delete an approved line from the ad and from
    the captions. Billing is unchanged — the clip floors at OMNI_DUR_MIN anyway.
    """
    from app.config import OMNI_DUR_MIN
    import app.pipeline as _p
    monkeypatch.setattr(_p, "OMNI_MULTISHOT", False)
    from app.pipeline import _omni_windows
    # Exactly at the shot ceiling, so it is neither floored nor divided — the
    # only beat under test here is the tiny one.
    long_enough = _tbeat(
        index=0, shot_type="talking_head",
        dialogue_text="one two three four five six")
    tiny = _tbeat(index=1, shot_type="talking_head", dialogue_text="hi")

    msgs: list[str] = []
    beats, windows, n_split = _omni_windows(
        [long_enough, tiny], on_progress=msgs.append)

    # Both beats survive, each still carrying its OWN line.
    assert [b.dialogue_text for b in beats] == [long_enough.dialogue_text, "hi"]
    assert windows[0][0] == 0.0
    assert windows[0][1] == pytest.approx(windows[1][0])       # contiguous
    assert windows[1][1] - windows[1][0] == pytest.approx(float(OMNI_DUR_MIN))
    assert any("floored" in m for m in msgs)


def test_omni_windows_never_mints_a_window_over_the_shot_ceiling(monkeypatch):
    """Every window must fit the fast-cut ceiling — the window is what assembly
    cuts to. Covers the brief-authored over-long hold (uncapped hold_s) beside a
    long line: a 14s "hold on the product" is a dead frame, and clamping it to
    the API's 10s ceiling (the pre-2026-08-05 behaviour) still left it dead."""
    from app.config import OMNI_DUR_MIN, OMNI_SHOT_MAX_S
    import app.pipeline as _p
    monkeypatch.setattr(_p, "OMNI_MULTISHOT", False)
    from app.pipeline import _omni_windows
    talky = _tbeat(index=0, shot_type="talking_head",
                   dialogue_text=" ".join(f"w{i}" for i in range(24)))
    long_hold = _tbeat(index=1, shot_type="broll", framing=None,
                       broll_type="lifestyle", beat_kind="reaction",
                       dialogue_text="", hold_s=14.0)

    beats, windows, _n = _omni_windows([talky, long_hold])

    for s, e in windows:
        assert OMNI_DUR_MIN <= (e - s) <= OMNI_SHOT_MAX_S
    assert windows[-1][1] - windows[-1][0] == pytest.approx(OMNI_SHOT_MAX_S)


# --- omni prompt builders: safety-critical clauses survive -------------------

@pytest.mark.asyncio
async def test_write_motion_prompts_omni_talk_carries_every_safety_clause():
    from app.pipeline import _write_motion_prompts

    voice_persona = SimpleNamespace(gender="female", age_band="mid-20s",
                                    timbre="warm bright", pace="calm",
                                    warmth="friendly")
    char = SimpleNamespace(voice_persona=voice_persona)
    beat = _tbeat(index=0, shot_type="talking_head",
                  dialogue_text="this changed my skin completely")

    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u0")], windows=[(0.0, 6.0)],
        gem=SimpleNamespace(), character=char, llm="gemini",
        product_ref_url=None, lipsync_engine="syncso", omni_mode=True,
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    rec = recs[0]
    assert rec["provider"] == "omni"
    assert rec["mode"] == "omni_talk"
    p = rec["prompt"]
    assert "this changed my skin completely" in p          # the dialogue line
    assert "warm bright" in p and "friendly" in p          # persona clause
    assert "single unbroken continuous shot" in p
    assert "never stretched" in p                          # anatomy guard core
    assert "No background music" in p


@pytest.mark.asyncio
async def test_write_motion_prompts_omni_vo_broll_carries_offscreen_line():
    from app.pipeline import _write_motion_prompts

    voice_persona = SimpleNamespace(gender="female", age_band="mid-20s",
                                    timbre="warm bright", pace="calm",
                                    warmth="friendly")
    char = SimpleNamespace(voice_persona=voice_persona)
    beat = _tbeat(index=0, shot_type="broll", framing=None,
                  broll_type="lifestyle", broll_shot="she pours the serum",
                  dialogue_text="it soaks in within seconds")

    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u0")], windows=[(0.0, 6.0)],
        gem=SimpleNamespace(), character=char, llm="gemini",
        product_ref_url=None, lipsync_engine="syncso", omni_mode=True,
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    rec = recs[0]
    assert rec["provider"] == "omni"
    assert rec["mode"] == "omni_vo_broll"
    p = rec["prompt"]
    assert "it soaks in within seconds" in p
    assert "single unbroken continuous shot" in p
    assert "No background music" in p


@pytest.mark.asyncio
async def test_write_motion_prompts_omni_silent_has_no_dialogue_no_music():
    from app.pipeline import _write_motion_prompts

    voice_persona = SimpleNamespace(gender="female", age_band="mid-20s",
                                    timbre="warm bright", pace="calm",
                                    warmth="friendly")
    char = SimpleNamespace(voice_persona=voice_persona)
    beat = _tbeat(index=0, shot_type="broll", framing=None,
                  broll_type="product_closeup", broll_shot="jar on the counter",
                  dialogue_text="")

    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u0")], windows=[(0.0, 5.0)],
        gem=SimpleNamespace(), character=char, llm="gemini",
        product_ref_url=None, lipsync_engine="syncso", omni_mode=True,
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    rec = recs[0]
    assert rec["provider"] == "omni"
    assert rec["mode"] == "omni_silent"
    assert "No dialogue, no music" in rec["prompt"]


@pytest.mark.asyncio
async def test_write_motion_prompts_omni_opus_path_hands_over_persona_and_spoken_line():
    """The Opus path (llm="claude" — the real default) is what ships omni
    prompts, so it must receive BOTH run-level inputs the deterministic builder
    has: the verbatim voice persona and the SPOKEN form of the line (tags
    stripped, emphasis de-CAPSed, respellings applied). Whatever the prompt
    quotes is literally what the model says."""
    from app.pipeline import _write_motion_prompts

    voice_persona = SimpleNamespace(gender="female", age_band="mid-20s",
                                    timbre="warm bright", pace="calm",
                                    warmth="friendly")
    char = SimpleNamespace(voice_persona=voice_persona)
    beat = _tbeat(index=0, shot_type="talking_head",
                  dialogue_text="[excited] Yaar this serum is INSANE, dekho na")
    seen: dict = {}

    async def fake_write(**kw):
        seen.update(kw)
        # A rewrite that keeps the line but paraphrases every safety clause away.
        return SimpleNamespace(
            prompt=f"She talks to the lens saying \"{kw['spoken_line']}\".",
            restaged=False)

    recs = await _write_motion_prompts(
        beats=[beat], anchors=[_anchor("u0")], windows=[(0.0, 6.0)],
        gem=SimpleNamespace(write_motion_prompt=fake_write), character=char,
        llm="claude", product_ref_url=None, lipsync_engine="syncso",
        omni_mode=True, pronunciations=[{"word": "dekho",
                                         "respelling": "day-kho"}],
        on_progress=lambda _m: None, manifest=_FakeManifest(), on_asset=None,
    )
    rec = recs[0]
    assert rec["source"] == "opus"
    # The persona clause reached the writer, verbatim.
    assert "warm bright" in seen["persona_clause"]
    assert "friendly" in seen["persona_clause"]
    # The line reached it in SPOKEN form: no [tag], no CAPS, respelled.
    assert seen["spoken_line"] == "Yaar this serum is insane, day-kho na"
    # And the paraphrased-away guards are all restored on the final prompt —
    # including the persona itself (identity, not a ban: only byte-identical
    # wording holds one voice across the run's clips).
    p = rec["prompt"]
    assert seen["persona_clause"] in p
    assert "no scene cuts" in p.lower()
    assert "No background music" in p
    assert "No on-screen text" in p
    assert "never stretched" in p                        # anatomy guard core


def test_omni_safety_tail_leaves_a_compliant_prompt_untouched():
    """The re-append must be additive-only: a brief that already carries every
    guard (and the persona verbatim) comes back byte-identical."""
    from app.gemini import render_voice_persona
    from app.pipeline import _omni_safety_tail, _omni_talking_prompt
    persona = render_voice_persona(
        SimpleNamespace(gender="female", age_band="mid-20s",
                        timbre="warm bright", pace="calm", warmth="friendly"),
        include_pace=True)
    beat = _tbeat(index=0, shot_type="talking_head", dialogue_text="hello there")
    built = _omni_talking_prompt(beat, persona, None, window_len=5.0)
    assert _omni_safety_tail(
        built, mode="omni_talk", persona_clause=persona) == built


def test_omni_safety_tail_restores_dropped_reference_clauses():
    """The reference clauses are Omni's IDENTITY anchors (no negative_prompt
    exists) — if Opus drops them, the safety tail must restore the right one
    per mode, plus the product ref on product beats (2026-08-01)."""
    from app.pipeline import (
        _OMNI_CHAR_REF_CLAUSE, _OMNI_PRODUCT_REF_CLAUSE,
        _OMNI_SCENE_REF_CLAUSE, _omni_safety_tail,
    )
    bare = ("A single unbroken continuous shot, no scene cuts: she smiles. "
            "No background music, no extra sound effects. "
            "No on-screen text, no logo, no captions.")
    talk = _omni_safety_tail(bare, mode="omni_talk")
    assert _OMNI_CHAR_REF_CLAUSE in talk
    broll = _omni_safety_tail(bare, mode="omni_silent", uses_product=True)
    assert _OMNI_SCENE_REF_CLAUSE in broll
    assert _OMNI_PRODUCT_REF_CLAUSE in broll
    # A prompt that already declares the references is left alone.
    assert _omni_safety_tail(talk, mode="omni_talk") == talk


def test_omni_talking_prompt_camera_follows_framing():
    """Omni fallback talking prompts must stage the beat's framing, not the
    hardcoded arm's-length selfie (2026-08-01) — Omni-mode screenplay variety
    died at this builder."""
    from app.pipeline import _omni_talking_prompt
    floor = _omni_talking_prompt(
        _tbeat(framing="floor_shot", dialogue_text="hello there"),
        "PERSONA.", None, window_len=5.0)
    assert "floor" in floor
    assert "arm's length" not in floor
    selfie = _omni_talking_prompt(
        _tbeat(framing="selfie", dialogue_text="hello there"),
        "PERSONA.", None, window_len=5.0)
    assert "arm's length" in selfie


def test_omni_vo_broll_quotes_the_line_with_double_quotes():
    """An apostrophe in the line must not be able to close the quoted span
    early — the model would read the remainder as prose direction."""
    from app.pipeline import _omni_vo_broll_prompt
    beat = _tbeat(index=0, shot_type="broll", framing=None,
                  broll_type="lifestyle", broll_shot="she pours the serum",
                  dialogue_text="it's honestly the only one I don't skip")
    p = _omni_vo_broll_prompt(beat, "PERSONA.", None, window_len=6.0)
    assert "\"it's honestly the only one I don't skip\"" in p


# --- _clip_request: the omni branch ------------------------------------------

def test_clip_request_omni_routes_to_omni_model_with_no_camera_lock():
    from app.config import OMNI_VIDEO_MODEL
    from app.pipeline import _clip_request
    beat = _tbeat()
    req = _clip_request(
        beat, anchor_url="https://cdn/anchor.png", duration=7, seed=42,
        native=False, prompt="p", audio_url=None, resolution="720p",
        omni=True, negative_prompt="should-be-dropped-not-an-omni-field",
    )
    assert req.model == OMNI_VIDEO_MODEL
    assert req.generate_audio is True             # the voice IS the deliverable
    assert req.seed == 42
    assert req.camera_fixed is False
    assert req.audio_url is None                  # Omni rejects reference audio
    assert req.negative_prompt is None            # Omni has no such field
    assert req.aspect_ratio == "9:16"
    assert req.duration == 7
    assert req.resolution == "720p"


def test_omni_duration_clamps_to_its_own_3_to_10_range():
    from app.config import OMNI_DUR_MAX, OMNI_DUR_MIN
    from app.pipeline import _omni_duration
    assert _omni_duration(0.5) == OMNI_DUR_MIN
    assert _omni_duration(50.0) == OMNI_DUR_MAX


def test_omni_duration_orders_headroom_above_the_window():
    """The request must exceed the planned speech length. Omni reads at
    1.89-2.92 w/s, so a beat sized at the average runs out of video mid-sentence
    on a clip nothing downstream can repair (2026-08-05: clips 0/4/7 had speech
    in their final frame). The surplus is trimmed out of the cut, not shipped."""
    from app.config import OMNI_SPEECH_HEADROOM_S
    from app.pipeline import _omni_duration
    for window in (3.0, 4.0, 6.2):
        assert _omni_duration(window) >= window + OMNI_SPEECH_HEADROOM_S


# --- generate_ugc_video, fully faked: the omni run as a whole ---------------

class _FakeVoiceSpy:
    """Records generate_vo calls — omni mode must NEVER call TTS."""
    def __init__(self):
        self.generate_vo_calls = 0

    async def generate_vo(self, *, script, voice_id, out_path, voice_vibe=None):
        self.generate_vo_calls += 1
        from app.voice import Alignment, VoiceResult
        Path(out_path).write_bytes(b"")
        return VoiceResult(audio_path=str(out_path),
                           alignment=Alignment(characters=["x"], starts=[0.0],
                                               ends=[1.0]))

    async def force_align(self, *, media_path, text):
        # Never actually invoked (assemble() is faked) — exists only so
        # getattr(voice, "force_align", None) is truthy when this client is
        # the one omni built, distinguishing it from voice=None.
        raise AssertionError("force_align must not run — assemble is faked")


def _omni_beats():
    talking = _beat(
        index=0, shot_type="talking_head",
        dialogue_text="this actually changed my skin so much for the better")
    vo_broll = _beat(
        index=1, shot_type="broll", framing=None, broll_type="lifestyle",
        broll_shot="she walks through the kitchen",
        dialogue_text="ok so here is the glow up right now",
        shows_creator=True)
    silent_broll = _beat(
        index=2, shot_type="broll", framing=None, broll_type="product_closeup",
        broll_shot="close on the jar on the counter", dialogue_text="",
        shows_creator=False)
    reaction = _beat(
        index=3, shot_type="broll", framing=None, broll_type="lifestyle",
        beat_kind="reaction", hold_s=5.0, dialogue_text="", shows_creator=True)
    return [talking, vo_broll, silent_broll, reaction]


async def _run_omni(tmp_path, monkeypatch, *, work, settings, **extra):
    """One fully-mocked omni generate_ugc_video run.

    Every paid client is faked; the video-order requests and the
    make_voice_client / make_lipsync_client call sites are all spied on so the
    "no TTS, no lipsync client, one run-level seed" contract is directly
    testable rather than inferred.
    """
    import app.pipeline as P

    calls: list = []
    beats = _omni_beats()
    timeline = _Rec(
        full_script=" ".join(b.dialogue_text for b in beats if b.dialogue_text),
        target_duration_s=None, script_source="generated",
        hook_type="bold_claim", narrative_structure="problem_solution",
        beats=beats,
    )

    requests: list = []

    class _SpyOmniVideoClient:
        async def create_task_async(self, request):
            requests.append(request)
            return f"task-{len(requests)}", {}

        async def poll_task_stream_async(self, task_id):
            yield "completed", SimpleNamespace(
                video_url=f"https://cdn/{task_id}.mp4")

    voice_spy = _FakeVoiceSpy()
    voice_client_calls: list = []

    def _make_voice_client_spy(s, provider=None):
        voice_client_calls.append(provider)
        return voice_spy

    lipsync_client_calls = {"n": 0}

    def _make_lipsync_client_spy(s, e=None):
        lipsync_client_calls["n"] += 1
        return object()

    monkeypatch.setattr(P, "GeminiClient",
                        lambda *a, **k: _RecGemini(calls, timeline))
    monkeypatch.setattr(P, "make_voice_client", _make_voice_client_spy)
    monkeypatch.setattr(P, "make_uploader", lambda s: _FakeUploader())
    monkeypatch.setattr(P, "make_image_client",
                        lambda s, u: _FakeAnchorClient(calls))
    monkeypatch.setattr(P, "make_video_client",
                        lambda s, vg=None: _SpyOmniVideoClient())
    monkeypatch.setattr(P, "make_lipsync_client", _make_lipsync_client_spy)
    monkeypatch.setattr(P, "download",
                        lambda url, local: Path(local).write_bytes(b"clip"))

    assemble_calls: list = []

    async def _fake_assemble(**kwargs):
        assemble_calls.append(kwargs)
        return SimpleNamespace(path=str(tmp_path / "out.mp4"),
                               duration_s=20.0, n_lipsynced=0)
    monkeypatch.setattr(P, "assemble", _fake_assemble)

    progress: list[str] = []
    result = await P.generate_ugc_video(
        brief="sell the thing", settings=settings, work_dir=work,
        out_path=tmp_path / "out.mp4", llm_provider="gemini",
        # visual_qc deliberately NOT pinned: the config default is opt-in
        # (False), so the harness exercises the real default and a caller can
        # pass visual_qc=True to test the ticked path.
        video_generator="omni",
        on_progress=progress.append,
        **extra,
    )
    import json as _json
    manifest = _json.loads((Path(work) / "manifest.json").read_text())
    return {
        "result": result, "requests": requests, "manifest": manifest,
        "beats": beats, "assemble_calls": assemble_calls,
        "voice_spy": voice_spy, "voice_client_calls": voice_client_calls,
        "lipsync_client_calls": lipsync_client_calls, "progress": progress,
    }


def _omni_settings(*, elevenlabs_key="x"):
    from app.config import Settings
    return Settings(
        gemini_api_key="x", anthropic_api_key="x",
        elevenlabs_api_key=elevenlabs_key,
        evolink_api_key="x", aws_access_key_id="x", aws_secret_access_key="x",
        aws_bucket_name="x", aws_region="x", sync_api_key="x",
    )


@pytest.mark.asyncio
async def test_generate_ugc_video_omni_never_calls_tts_and_skips_lipsync(
    tmp_path, monkeypatch,
):
    """No TTS, no lipsync client, one run-level seed across every clip, every
    duration inside Omni's own 3-10s range, no reference audio, and the
    manifest records voice.provider == 'omni-native' / params.video_generator
    == 'omni'."""
    import app.pipeline as P

    work = tmp_path / "work"
    out = await _run_omni(tmp_path, monkeypatch, work=work,
                          settings=_omni_settings())

    # No TTS was ever generated.
    assert out["voice_spy"].generate_vo_calls == 0
    # The only voice client built was the captions force-aligner.
    assert out["voice_client_calls"] == ["elevenlabs"]
    # No lipsync client was constructed at all — omni_mode skips the call site.
    assert out["lipsync_client_calls"]["n"] == 0

    manifest = out["manifest"]
    assert manifest["voice"]["provider"] == "omni-native"
    assert manifest["params"]["video_generator"] == "omni"

    # One run-level seed for every clip (not a per-beat seed). The clip COUNT is
    # whatever the shot bar and the multi-shot fold settle on — asserted against
    # the planned windows rather than a literal, so a pacing change does not read
    # as a seeding regression.
    reqs = out["requests"]
    assert len(reqs) == len(manifest["beat_windows"])
    expected_seed = P._seed_for(work, 0)
    assert all(r.seed == expected_seed for r in reqs)
    # Every clip stays inside Omni's own 3-10s range, no reference audio.
    from app.config import OMNI_DUR_MAX, OMNI_DUR_MIN
    for r in reqs:
        assert OMNI_DUR_MIN <= r.duration <= OMNI_DUR_MAX
        assert r.audio_url is None
        assert r.model == P.OMNI_VIDEO_MODEL

    # assemble() rode the native_lipsync path with no lipsync client.
    assert out["assemble_calls"][0]["native_lipsync"] is True
    assert out["assemble_calls"][0]["lipsync_client"] is None
    assert out["result"].n_lipsynced == 0


@pytest.mark.asyncio
async def test_generate_ugc_video_omni_clamps_1080p_to_720p(tmp_path, monkeypatch):
    work = tmp_path / "work"
    out = await _run_omni(tmp_path, monkeypatch, work=work,
                          settings=_omni_settings(), clip_resolution="1080p")
    assert out["manifest"]["params"]["clip_resolution"] == "720p"
    assert any("clamped to 720p" in m for m in out["progress"])


@pytest.mark.asyncio
async def test_generate_ugc_video_omni_offers_force_align_when_key_present(
    tmp_path, monkeypatch,
):
    """With an ElevenLabs key, omni still builds a voice client — used ONLY as
    the captions force-aligner against the finished video."""
    work = tmp_path / "work"
    out = await _run_omni(tmp_path, monkeypatch, work=work,
                          settings=_omni_settings(elevenlabs_key="x"))
    assert out["voice_client_calls"] == ["elevenlabs"]
    # assemble() only ever sees the force-aligner, never a generate_vo call —
    # getattr(voice, "force_align", None) is truthy exactly when this client
    # (not None) was built.
    assert out["assemble_calls"][0]["force_align"] is not None


@pytest.mark.asyncio
async def test_generate_ugc_video_omni_completes_with_captions_skipped_when_key_blank(
    tmp_path, monkeypatch,
):
    """A blank ElevenLabs key must NOT raise in omni mode (unlike every other
    mode) — the run completes and captions are skipped, gracefully."""
    work = tmp_path / "work"
    out = await _run_omni(tmp_path, monkeypatch, work=work,
                          settings=_omni_settings(elevenlabs_key=""))
    assert out["voice_client_calls"] == []          # never built
    assert out["assemble_calls"][0]["force_align"] is None
    assert any("captions will be skipped" in m for m in out["progress"])
    assert out["result"].final_video_path == str(tmp_path / "out.mp4")


@pytest.mark.asyncio
async def test_generate_ugc_video_omni_ignores_a_stale_seedance_native_selection(
    tmp_path, monkeypatch,
):
    """The lipsync radio's value still arrives on an omni run (the UI only greys
    the widget out), and omni has NO voiceover to slice a timbre anchor from —
    building one would hand ffmpeg the literal string "None" AFTER the run's
    LLM work was paid for. Every seedance_native branch must be omni-guarded."""
    work = tmp_path / "work"
    out = await _run_omni(tmp_path, monkeypatch, work=work,
                          settings=_omni_settings(),
                          lipsync_engine="seedance_native")
    assert "timbre_anchor_url" not in out["manifest"]
    # No reference audio ever reached a clip request either.
    assert all(r.audio_url is None for r in out["requests"])
    assert out["result"].final_video_path == str(tmp_path / "out.mp4")


@pytest.mark.asyncio
async def test_generate_ugc_video_omni_tells_assemble_the_clips_carry_ambience(
    tmp_path, monkeypatch,
):
    """Omni authors audio for every clip (silent beats are briefed with named
    room tone), so non-speaking beats must keep their own track instead of the
    synthetic silence the audio-less seedance path needs."""
    work = tmp_path / "work"
    out = await _run_omni(tmp_path, monkeypatch, work=work,
                          settings=_omni_settings())
    kwargs = out["assemble_calls"][0]
    assert kwargs["native_lipsync"] is True
    assert kwargs["native_ambient_audio"] is True


import asyncio

import app.pipeline as p
from app.config import OMNI_EVOLINK_MODEL

# ── Omni's EvoLink fallback (_order_clip's second route) ─────────────
# 2026-08-05: a 10-clip Omni run lost clips [0, 3, 9] to a bare
# 400 invalid_request from Google, and _gather_with_retry could only re-poll the
# same rejected interaction. The same model is resold on EvoLink, so a refused
# clip is re-ordered there instead of killing a run that already paid for its
# siblings.

class _FakeRouteClient:
    """One video route. Fails create/poll on demand, records what it saw."""

    def __init__(self, name, *, fail_create=False, fail_poll=False,
                 duration_supported=True):
        self.name = name
        self.fail_create = fail_create
        self.fail_poll = fail_poll
        self.duration_supported = duration_supported
        self.created: list = []
        self.polled: list[str] = []

    async def create_task_async(self, request):
        self.created.append(request)
        if self.fail_create:
            raise RuntimeError(f"{self.name} create refused: 400 invalid_request")
        return f"{self.name}-task-{len(self.created)}", {}

    async def poll_task_stream_async(self, task_id, max_wait_seconds=None):
        self.polled.append(task_id)
        if self.fail_poll:
            raise RuntimeError(
                f"Omni poll failed (non-retryable): BadRequestError: "
                f"Error code: 400 - {{'error': {{'code': 'invalid_request'}}}}"
            )
        yield "completed", SimpleNamespace(
            video_url=f"https://cdn/{task_id}.mp4", raw={})


async def _order(primary, fallback_client, *, existing=None, tmp_path=None,
                 monkeypatch=None):
    """Run _order_clip with the download and the ledger stubbed out."""
    ids: list[str] = []
    monkeypatch.setattr(p, "download", lambda url, dest: Path(dest).write_text("mp4"))
    req = _clip_request(
        _beat(), anchor_url="https://cdn/anchor-0.png", duration=4, seed=7,
        native=False, prompt="she smiles and speaks", audio_url=None,
        resolution="720p", omni=True,
    )
    out = await p._order_clip(
        primary, req, index=0, dest=tmp_path / "clip-0.mp4",
        on_task_id=ids.append, existing_task_id=existing, provider="omni",
        used_seconds=4.0, fallback=p._omni_fallback(req, fallback_client),
        on_progress=lambda m: None,
    )
    return out, ids


@pytest.mark.asyncio
async def test_omni_clip_falls_back_to_evolink_when_google_refuses_the_poll(
    tmp_path, monkeypatch,
):
    google = _FakeRouteClient("google", fail_poll=True)
    evolink = _FakeRouteClient("evolink")
    (task_id, url, provider), _ids = await _order(
        google, evolink, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert provider == "omni_evolink"
    assert task_id.startswith("evolink-task")
    assert url.endswith(".mp4")
    # The clip was re-ordered on the second route, with the EvoLink model slug.
    assert len(evolink.created) == 1
    assert evolink.created[0].model == OMNI_EVOLINK_MODEL
    # ...and the prompt/anchor/duration are the SAME clip, not a fresh plan.
    assert evolink.created[0].prompt == google.created[0].prompt
    assert evolink.created[0].first_frame_url == google.created[0].first_frame_url
    assert evolink.created[0].duration == google.created[0].duration


@pytest.mark.asyncio
async def test_omni_clip_falls_back_when_google_refuses_the_create(
    tmp_path, monkeypatch,
):
    google = _FakeRouteClient("google", fail_create=True)
    evolink = _FakeRouteClient("evolink")
    (_task_id, _url, provider), _ids = await _order(
        google, evolink, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert provider == "omni_evolink"
    assert len(evolink.created) == 1


@pytest.mark.asyncio
async def test_omni_clip_without_a_fallback_still_raises(tmp_path, monkeypatch):
    """The pre-fallback contract, kept for OMNI_EVOLINK_FALLBACK=0: a refused
    clip must NOT be silently swallowed when there is no second route."""
    google = _FakeRouteClient("google", fail_poll=True)
    with pytest.raises(RuntimeError, match="Omni poll failed"):
        await _order(google, None, tmp_path=tmp_path, monkeypatch=monkeypatch)


@pytest.mark.asyncio
async def test_omni_fallback_task_id_is_tagged_with_its_route(
    tmp_path, monkeypatch,
):
    """One manifest slot holds ids from two APIs, so the fallback's id carries
    its route — otherwise the retry hands an EvoLink id to Google (or back)."""
    google = _FakeRouteClient("google", fail_poll=True)
    evolink = _FakeRouteClient("evolink")
    _out, ids = await _order(google, evolink, tmp_path=tmp_path,
                             monkeypatch=monkeypatch)
    assert ids[0] == "google-task-1"                     # primary: untagged
    assert ids[-1] == "omni_evolink:evolink-task-1"       # fallback: tagged


@pytest.mark.asyncio
async def test_a_tagged_task_id_is_polled_on_the_fallback_route_only(
    tmp_path, monkeypatch,
):
    """The retry path: a clip whose EvoLink create was accepted before something
    downstream failed must re-poll EvoLink, never Google."""
    google = _FakeRouteClient("google")
    evolink = _FakeRouteClient("evolink")
    (task_id, _url, provider), _ids = await _order(
        google, evolink, existing="omni_evolink:evolink-task-9",
        tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert (task_id, provider) == ("evolink-task-9", "omni_evolink")
    assert evolink.polled == ["evolink-task-9"]
    assert google.polled == [] and google.created == []


@pytest.mark.asyncio
async def test_a_tagged_task_id_without_its_route_refuses_to_guess(
    tmp_path, monkeypatch,
):
    google = _FakeRouteClient("google")
    with pytest.raises(RuntimeError, match="not available on this run"):
        await _order(google, None, existing="omni_evolink:evolink-task-9",
                     tmp_path=tmp_path, monkeypatch=monkeypatch)


def test_a_fallen_back_clip_is_priced_at_evolinks_rate_not_googles():
    """Both routes bill in dollars per second but at DIFFERENT rates, so the
    per-run estimate has to read the provider the clip actually used."""
    est = p._clip_cost_estimate({
        0: {"provider": "omni", "duration_requested": 10},
        1: {"provider": "omni_evolink", "duration_requested": 10},
    })
    assert est["by_provider"]["omni"]["usd"] == pytest.approx(1.00)
    assert est["by_provider"]["omni_evolink"]["usd"] == pytest.approx(0.87)
    assert est["total_credits"] == 0     # neither route is credit-metered


# The fallback trigger is EVERY primary failure, not just a 400: bad request,
# quota/rate limits, an overloaded provider that outlived the poller's own
# retries, the 20-minute deadline, a dead anchor fetch, a completed task with no
# artifact. If Google could not deliver the clip, EvoLink is asked for it.
@pytest.mark.parametrize("exc", [
    RuntimeError("Omni poll failed (non-retryable) for interaction abc123: "
                 "BadRequestError: Error code: 400 - invalid_request"),
    RuntimeError("429 RESOURCE_EXHAUSTED. Your prepayment credits are depleted"),
    RuntimeError("Omni poll failed after 6 retries: InternalServerError: 503"),
    RuntimeError("Omni interaction abc123 exceeded the 1200s poll deadline"),
    RuntimeError("Omni interaction abc123 completed without an output video"),
    ValueError("Omni has no audio input"),
    OSError("anchor fetch failed"),
    TimeoutError("read timeout"),
])
@pytest.mark.asyncio
async def test_every_primary_failure_class_falls_back_to_evolink(
    exc, tmp_path, monkeypatch,
):
    class _Boom(_FakeRouteClient):
        async def create_task_async(self, request):
            await super().create_task_async(request)
            raise exc

    evolink = _FakeRouteClient("evolink")
    (_task_id, _url, provider), _ids = await _order(
        _Boom("google"), evolink, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert provider == "omni_evolink"
    assert len(evolink.created) == 1


@pytest.mark.asyncio
async def test_a_cancelled_run_does_not_trigger_a_paid_fallback(
    tmp_path, monkeypatch,
):
    """CancelledError is BaseException, deliberately outside the except: a user
    who killed the run must not have every in-flight clip re-ordered (and
    re-billed) on the way out."""
    class _Cancelled(_FakeRouteClient):
        async def create_task_async(self, request):
            raise asyncio.CancelledError()

    evolink = _FakeRouteClient("evolink")
    with pytest.raises(asyncio.CancelledError):
        await _order(_Cancelled("google"), evolink, tmp_path=tmp_path,
                     monkeypatch=monkeypatch)
    assert evolink.created == []


@pytest.mark.asyncio
async def test_a_failing_fallback_surfaces_its_own_error_not_the_primarys(
    tmp_path, monkeypatch,
):
    """Both routes down: the raise must name what EvoLink said, since that is
    the second and final attempt — the primary's error is already logged."""
    google = _FakeRouteClient("google", fail_poll=True)
    evolink = _FakeRouteClient("evolink", fail_create=True)
    with pytest.raises(RuntimeError, match="evolink create refused"):
        await _order(google, evolink, tmp_path=tmp_path, monkeypatch=monkeypatch)


# ── Omni in-clip multi-shot: fold beats into one take, cuts inside ────
# The screenplay decides what each shot shows; Omni changes scene natively at its
# own cuts, so several beats ride in ONE generation with the dialogue running
# straight through. The shot ceiling is enforced on SHOTS (2-3s), never on the
# take — a take may run the full OMNI_DUR_MAX.

def _grouped(beats, product_ref_url=None):
    from app.pipeline import _omni_windows
    out, wins, _n = _omni_windows(beats, product_ref_url=product_ref_url,
                                  on_progress=lambda m: None)
    return out, wins


def test_multishot_folds_consecutive_beats_into_one_take():
    from app.config import OMNI_DUR_MAX, OMNI_SHOT_MAX_S
    a = _tbeat(index=0, ad_section="problem", shot_type="talking_head",
               dialogue_text="one two three four five six")
    b = _tbeat(index=1, ad_section="problem", shot_type="broll",
               broll_type="lifestyle", broll_shot="bread on the counter",
               dialogue_text="seven eight nine ten eleven twelve")

    out, wins = _grouped([a, b])

    assert len(out) == 1                       # one generation, two shots
    take = out[0]
    assert len(take.shots) == 2
    # Every SHOT stays inside the fast-cut bar...
    assert max(take.shot_lens) <= OMNI_SHOT_MAX_S
    # ...while the TAKE is allowed to be long (that is the whole point).
    assert (wins[0][1] - wins[0][0]) <= OMNI_DUR_MAX
    # The spoken text is preserved in order — captions and the fidelity clause
    # both read dialogue_text, so a fold must never drop or reorder words.
    assert take.dialogue_text == f"{a.dialogue_text} {b.dialogue_text}"


def test_multishot_take_never_exceeds_the_api_ceiling():
    from app.config import OMNI_DUR_MAX
    beats = [_tbeat(index=i, ad_section="problem", shot_type="talking_head",
                    dialogue_text="one two three four five six")
             for i in range(8)]

    _out, wins = _grouped(beats)

    for s, e in wins:
        assert (e - s) <= OMNI_DUR_MAX


def test_multishot_leaves_the_hook_alone():
    """The first seconds decide whether the ad is watched, so the hook keeps its
    own fully-directed generation on its own rendered anchor."""
    hook = _tbeat(index=0, ad_section="hook", shot_type="talking_head",
                  dialogue_text="one two three four five six")
    nxt = _tbeat(index=1, ad_section="problem", shot_type="talking_head",
                 dialogue_text="seven eight nine ten eleven twelve")

    out, _wins = _grouped([hook, nxt])

    assert len(out) == 2
    assert not getattr(out[0], "shots", None)


def test_multishot_never_folds_a_product_beat():
    """A product beat's anchor is rendered WITH the real product; a generation
    carries one reference image, so a product shot folded into someone else's
    take would have no product to reference — the exact hallucination the
    product-grounding rule exists to prevent."""
    talk = _tbeat(index=0, ad_section="solution", shot_type="talking_head",
                  dialogue_text="one two three four five six")
    prod = _tbeat(index=1, ad_section="solution", shot_type="broll",
                  broll_type="product_closeup",
                  broll_shot="the bottle on the counter",
                  dialogue_text="seven eight nine ten eleven twelve")

    out, _wins = _grouped([talk, prod], product_ref_url="https://cdn/p.png")

    assert len(out) == 2
    assert not getattr(out[1], "shots", None)


def test_multishot_prompt_carries_one_screenplay_scene_per_timecode_block():
    """Each block is the BEAT'S OWN scene, not a generic reframe: the screenplay
    and brief decide what every shot shows."""
    from app.pipeline import _omni_multishot_prompt
    a = _tbeat(index=0, ad_section="problem", shot_type="talking_head",
               framing="confessional", visual_note="she sits on the bed",
               dialogue_text="one two three four five six")
    b = _tbeat(index=1, ad_section="problem", shot_type="broll",
               broll_type="lifestyle", broll_shot="bread on the kitchen counter",
               dialogue_text="seven eight nine ten eleven twelve")
    out, wins = _grouped([a, b])
    take = out[0]

    prompt = _omni_multishot_prompt(take, "PERSONA.",
                                    window_len=wins[0][1] - wins[0][0])

    assert "[0-" in prompt and "s]" in prompt          # timecode blocks
    assert "she sits on the bed" in prompt             # beat 0's own scene
    assert "bread on the kitchen counter" in prompt    # beat 1's own scene
    # The line is one continuous take across the cuts, verbatim.
    assert "one two three four five six seven eight nine ten eleven twelve" in prompt
    assert "never restarts, pauses or repeats" in prompt
    # Trial 2's fix: the voice is placed shot by shot, so an insert stops
    # implying "she is on camera talking".
    assert "off-camera voiceover" in prompt
    assert "INSERT CUTAWAY" in prompt
    assert "OUT OF FRAME" in prompt
    # Scene changes are REQUIRED, and only additions are banned.
    assert "the scene must genuinely change at each cut" in prompt
    assert "Do not add any scene, location, person or product" in prompt
    # A folded take must never also carry the single-shot instruction.
    assert "single unbroken continuous shot" not in prompt


def test_multishot_routes_a_folded_beat_to_the_multishot_builder():
    from app.pipeline import _fallback_prompt
    a = _tbeat(index=0, ad_section="problem", shot_type="talking_head",
               dialogue_text="one two three four five six")
    b = _tbeat(index=1, ad_section="problem", shot_type="talking_head",
               dialogue_text="seven eight nine ten eleven twelve")
    out, wins = _grouped([a, b])

    prompt = _fallback_prompt(out[0], wins[0][1] - wins[0][0],
                              lipsync_engine="syncso", omni_mode=True,
                              persona_clause="PERSONA.")

    assert "shots in one continuous" in prompt
    assert "single unbroken continuous shot" not in prompt


def test_multishot_off_restores_separate_clips(monkeypatch):
    import app.pipeline as _p
    monkeypatch.setattr(_p, "OMNI_MULTISHOT", False)
    a = _tbeat(index=0, ad_section="problem", shot_type="talking_head",
               dialogue_text="one two three four five six")
    b = _tbeat(index=1, ad_section="problem", shot_type="talking_head",
               dialogue_text="seven eight nine ten eleven twelve")

    out, _wins = _grouped([a, b])

    assert len(out) == 2
    assert not any(getattr(x, "shots", None) for x in out)


# ── Product reference attachment (both generators) ───────────────────
# HARD RULE (2026-08-06): whenever the product is on screen in ANY way — held,
# used, on a counter, or just in the background of a talking head — the real
# product photo must be attached, or the model invents a lookalike with the wrong
# label. Both paths ride this: _beat_uses_product gates _anchor_refs, and the
# anchor is the first frame for Seedance AND Omni alike.

def test_product_ref_attaches_when_the_planner_says_it_is_in_frame():
    """The screenplay's own declaration wins. A plain selfie talking-head with
    the bottle on the counter behind her got NO product ref before this — the
    old rule only fired on framing=product_reveal_pivot."""
    from app.pipeline import _beat_uses_product
    beat = _beat(shot_type="talking_head", framing="selfie",
                 visual_note="she talks to the lens", product_in_frame=True)
    assert _beat_uses_product(beat, "https://cdn/p.png") is True


def test_product_ref_attaches_when_the_screenplay_text_names_the_product():
    """Backstop for a planner that forgot the flag, and for manifests written
    before the field existed (they validate with product_in_frame=False)."""
    from app.pipeline import _beat_uses_product
    beat = _beat(shot_type="talking_head", framing="tripod",
                 visual_note="she talks with the Gastro Pro bottle on the "
                             "counter behind her")
    assert _beat_uses_product(beat, "https://cdn/p.png",
                              "IBS Gastro Pro") is True


def test_product_ref_attaches_on_a_product_noun_even_without_a_name():
    """Deliberately over-triggers: a false positive costs one extra reference
    image, a false negative ships an invented product."""
    from app.pipeline import _beat_uses_product
    beat = _beat(shot_type="broll", framing=None, broll_type="lifestyle",
                 broll_shot="the softgel strip resting on the shelf",
                 visual_note="morning light on the shelf")
    assert _beat_uses_product(beat, "https://cdn/p.png") is True


@pytest.mark.parametrize("kwargs", [
    dict(shot_type="talking_head", framing="product_reveal_pivot"),
    dict(shot_type="broll", framing=None, broll_type="product_closeup"),
    dict(shot_type="broll", framing=None, broll_type="demo_steps"),
])
def test_product_ref_keeps_the_old_structural_rules(kwargs):
    from app.pipeline import _beat_uses_product
    beat = _beat(visual_note="a staged moment", **kwargs)
    assert _beat_uses_product(beat, "https://cdn/p.png") is True


def test_no_product_ref_on_a_genuinely_product_free_beat():
    from app.pipeline import _beat_uses_product
    beat = _beat(shot_type="broll", framing=None, broll_type="lifestyle",
                 broll_shot="she pulls the curtains open",
                 visual_note="sunlight fills the bedroom")
    assert _beat_uses_product(beat, "https://cdn/p.png") is False


def test_no_product_ref_when_the_run_has_no_product_image():
    """Nothing to attach — and the flag must not conjure one."""
    from app.pipeline import _beat_uses_product
    beat = _beat(shot_type="talking_head", framing="selfie",
                 product_in_frame=True)
    assert _beat_uses_product(beat, None) is False


def test_anchor_refs_attaches_the_product_photo_for_a_flagged_talking_head():
    """The both-paths guarantee: the anchor carries the product reference, and
    the anchor is the first frame for Seedance and Omni alike."""
    from app.pipeline import _anchor_refs
    beat = _beat(shot_type="talking_head", framing="lav_mic",
                 visual_note="she talks, product on the counter",
                 product_in_frame=True)

    refs, roles = _anchor_refs(beat, "https://cdn/char.png",
                               "https://cdn/prod.png", "IBS Gastro Pro")

    assert "https://cdn/prod.png" in refs
    assert roles[refs.index("https://cdn/prod.png")] == "product"


def test_product_noun_match_is_word_boundaried_not_substring():
    """Regression: 'tin' matched inside 'hinting', so a talking beat with no
    product in it acquired a product reference (found on the real 2026-08-05
    script, where BOTH noun hits were false positives)."""
    from app.pipeline import _mentions_product
    beat = _beat(shot_type="talking_head", framing="front_pano",
                 visual_note="hinting there's more to come")
    assert _mentions_product(beat, None) is False


def test_product_nouns_never_override_a_known_product_name():
    """The ailment described in a problem-section beat is not the product: 'a
    strip of medicine tablets' on the counter must NOT pull the product photo
    into the problem shot."""
    from app.pipeline import _beat_uses_product
    beat = _beat(shot_type="broll", framing=None, broll_type="lifestyle",
                 ad_section="problem",
                 broll_shot="a sugary snack, bread and a strip of medicine "
                            "tablets sit in the morning kitchen light",
                 visual_note="a quick push-in on the counter")
    assert _beat_uses_product(beat, "https://cdn/p.png",
                              "IBS Gastro Pro") is False
    # With no name to match on, the nouns are all we have — then it fires.
    assert _beat_uses_product(beat, "https://cdn/p.png", None) is True


@pytest.mark.asyncio
async def test_a_folded_take_is_written_by_opus_and_handed_the_shot_plan(tmp_path):
    """Opus writes the take's prompt — it is just not allowed to invent the
    shots. On the 2026-08-06 live run it was NOT told about the fold, obeyed the
    omni skill doc's single-unbroken-shot lock, and both takes came back as one
    uncut 7s/9s shot. So it now receives the screenplay's shot plan (scenes,
    order, cut times) and the mode that overrides the lock."""
    from types import SimpleNamespace
    import app.pipeline as _p

    a = _beat(index=0, ad_section="problem", shot_type="talking_head",
              dialogue_text="one two three four five six")
    b = _beat(index=1, ad_section="problem", shot_type="broll", framing=None,
              broll_type="lifestyle", broll_shot="bread on the counter",
              dialogue_text="seven eight nine ten eleven twelve")
    beats, wins, _n = _p._omni_windows([a, b], on_progress=lambda m: None)
    assert len(beats) == 1 and len(beats[0].shots) == 2      # it folded

    seen: dict = {}

    class _GemSpy:
        async def write_motion_prompt(self, **kw):
            seen.update(kw)
            blocks = " ".join(f"[{sh['start']}-{sh['end']}s] {sh['label']}: "
                              f"{sh['scene']}" for sh in kw["shot_plan"])
            # Must carry the spoken line verbatim, or the line-survival guard
            # (rightly) discards the sample and uses the deterministic prompt.
            line = kw["spoken_line"] or kw["beat"].dialogue_text
            return SimpleNamespace(
                prompt=(f"opus wrote this. {blocks} "
                        f"Audio: she says out loud: \"{line}\". "
                        f"No background music, no extra sound effects."),
                restaged=False)

    records = await _p._write_motion_prompts(
        beats=beats, anchors=[SimpleNamespace(url="https://cdn/a.png")],
        windows=wins, gem=_GemSpy(),
        character=SimpleNamespace(voice_persona=SimpleNamespace(
            gender="female", age_band="mid-20s", timbre="warm",
            pace="brisk", warmth="friendly")),
        llm="claude", product_ref_url=None, lipsync_engine="syncso",
        omni_mode=True, on_progress=lambda m: None,
        manifest=SimpleNamespace(add_step=lambda *a, **k: None,
                                 update=lambda *a, **k: None),
        on_asset=None,
    )

    # Opus WAS used, and was told this is a multi-shot take.
    assert seen["mode"] == "omni_multishot"
    plan = seen["shot_plan"]
    assert [ (p["start"], p["end"]) for p in plan ] == [(0, 3), (3, 6)]
    # Each entry carries its shot kind, and the cutaway names its exclusion —
    # that is what makes Omni render an insert instead of another talking angle.
    assert plan[0]["label"] == "ON CAMERA, SPEAKING"
    assert plan[1]["label"].startswith("INSERT CUTAWAY")
    assert "OUT OF FRAME" in plan[1]["scene"]
    # The screenplay's own scene text, not a generic reframe.
    assert "bread on the counter" in plan[1]["scene"]
    assert records[0]["prompt"].startswith("opus wrote this")


@pytest.mark.asyncio
async def test_a_multishot_prompt_missing_its_timecode_blocks_is_rejected():
    """The cut structure is verified, not trusted: a sample that dropped the
    blocks (or kept the single-shot lock) is a failed sample and must retry."""
    from types import SimpleNamespace
    from app.config import Settings
    from app.gemini import GeminiClient

    plan = [{"start": 0, "end": 3, "label": "ON CAMERA, SPEAKING", "scene": "x"},
            {"start": 3, "end": 6, "label": "INSERT CUTAWAY", "scene": "y"}]

    class _Flat:
        """Returns a prompt with no shot list — the live 2026-08-06 failure."""
        async def _generate(self, **kw):
            return SimpleNamespace(
                prompt="In a single unbroken continuous shot, she talks.",
                restaged=False)

    gem = GeminiClient.__new__(GeminiClient)
    gem._generate = _Flat()._generate
    gem._load_omni_skill_md = lambda: "SKILL"
    gem._load_skill_md = lambda: "SKILL"
    errors: list = []

    async def _with_retry(fn, *, label):
        try:
            return await fn()
        except ValueError as e:
            errors.append(str(e))
            raise

    gem._with_retry = _with_retry
    beat = SimpleNamespace(index=0, ad_section="problem",
                           shot_type="talking_head", framing="confessional",
                           broll_type=None, dialogue_text="one two three",
                           visual_note="she sits")
    char = SimpleNamespace(age=25, gender="female", ethnicity="Indian",
                           hair="bun", face="warm", wardrobe="tee",
                           setting="kitchen", energy="warm")
    with pytest.raises(ValueError):
        await gem.write_motion_prompt(
            beat=beat, character=char, window_len=6.0, mode="omni_multishot",
            uses_product=False, anchor_url="https://cdn/a.png",
            timeline_overview="ov", fallback_example="fb", shot_plan=plan)
    assert "timecode block" in errors[0]


@pytest.mark.asyncio
async def test_ai_review_is_opt_in_and_skipped_by_default(tmp_path, monkeypatch):
    """The Gemini defect audit is the LAST thing a run does, costs a multimodal
    pass over the whole finished master, and reports rather than repairs — so it
    only runs when the operator ticks "AI review" in the UI (2026-08-06)."""
    from app.config import VISUAL_QC
    import app.qc as _qc

    # The config value is what both UI checkboxes initialise to, so an opt-in
    # default is what leaves them unchecked.
    assert VISUAL_QC is False

    seen: list = []

    async def _spy(path, settings, *, duration=0.0):
        seen.append(path)
        return {"verdict": "pass", "issues": []}

    monkeypatch.setattr(_qc, "review_video", _spy)
    monkeypatch.setattr(_qc, "summarize", lambda r: "AI review: ok")

    await _run_omni(tmp_path, monkeypatch, work=tmp_path / "off",
                    settings=_omni_settings())
    assert seen == [], "the review ran without being asked for"

    await _run_omni(tmp_path, monkeypatch, work=tmp_path / "on",
                    settings=_omni_settings(), visual_qc=True)
    assert len(seen) == 1, "ticking AI review did not run it"
