from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from app.gemini import (
    GeminiClient, ScriptDraft, ChunkSpec, CharacterDescription,
    Screenplay, SceneBeat, VideoTimeline, TimelineBeat,
    VoicePersona, render_voice_persona,
)
from app.config import Settings, GEMINI_FAST_MODEL, GEMINI_PRO_MODEL


def _settings():
    # These tests exercise the Gemini path explicitly — the provider default
    # is "claude" now.
    return Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="gem-test",
        elevenlabs_api_key="el", sync_api_key="sy", llm_provider="gemini",
    )


def _mock_genai_client(text_value: str):
    """Return a MagicMock that mimics google-genai's client.aio.models.generate_content."""
    fake_resp = MagicMock(text=text_value)
    fake = MagicMock()
    fake.aio.models.generate_content = AsyncMock(return_value=fake_resp)
    return fake


def _char(**overrides) -> CharacterDescription:
    """A minimal valid CharacterDescription for the director's new signature."""
    fields = dict(
        age=26, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter", expression="calm_mid_sentence",
        hair="long dark waves", face="warm eyes, soft smile",
        wardrobe="fitted cream tank, gold hoops", energy="warm kitchen energy",
        voice_vibe="warm_relatable",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )
    fields.update(overrides)
    return CharacterDescription(**fields)


@pytest.mark.asyncio
async def test_plan_timeline_parses_and_uses_pro_model():
    tl = {
        "full_script": "Okay so I tried this. And honestly it works.",
        "target_duration_s": 30,
        "hook_type": "result_first",
        "narrative_structure": "testimonial",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "Okay so I tried this. ",
             "shot_type": "talking_head", "framing": "lav_mic",
             "broll_type": None, "broll_shot": None,
             "visual_note": "creator w/ mic"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "And honestly it works.",
             "shot_type": "broll", "framing": None,
             "broll_type": "hands_using", "broll_shot": "hands holding it",
             "visual_note": "product close-up"},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)

    result = await c.plan_timeline(
        brief="a skincare serum", character=_char(), product_present=True,
        target_duration_s=30,
    )

    assert isinstance(result, VideoTimeline)
    assert result.hook_type == "result_first"
    assert result.narrative_structure == "testimonial"
    assert result.beats[0].ad_section == "hook"
    assert result.beats[0].framing == "lav_mic"
    assert result.beats[1].broll_type == "hands_using"
    assert result.beats[-1].ad_section == "cta"
    assert "".join(b.dialogue_text for b in result.beats) == result.full_script
    kw = fake.aio.models.generate_content.call_args.kwargs
    # Script-writing now runs on Pro (was flash) at a higher temperature.
    assert kw["model"] == GEMINI_PRO_MODEL
    assert kw["config"].temperature == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_plan_timeline_overrides_forced():
    tl = {
        "full_script": "Stop scrolling right now please. This serum changed my skin.",
        "target_duration_s": 30, "hook_type": "stop_scrolling",
        "narrative_structure": "pas",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "Stop scrolling right now please.",
             "shot_type": "talking_head", "framing": "approach",
             "broll_type": None, "broll_shot": None,
             "visual_note": "walks toward camera"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": " This serum changed my skin.",
             "shot_type": "talking_head", "framing": "selfie",
             "broll_type": None, "broll_shot": None,
             "visual_note": "cta"},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)
    await c.plan_timeline(
        brief="serum", character=_char(), product_present=True,
        target_duration_s=30,
        hook_type="stop_scrolling", narrative_structure="pas",
    )
    # The forced choices are injected into the prompt.
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    assert "FORCED CHOICES" in contents
    assert "stop_scrolling" in contents and "pas" in contents


@pytest.mark.asyncio
async def test_plan_timeline_rejects_bad_anatomy():
    """A timeline whose last beat isn't a CTA must trigger a retry/raise."""
    tl = {
        "full_script": "hi there friend",
        "target_duration_s": 30, "hook_type": "question",
        "narrative_structure": "pas",
        "beats": [
            {"index": 0, "ad_section": "problem", "dialogue_text": "hi there friend",
             "shot_type": "talking_head", "framing": "selfie",
             "broll_type": None, "broll_shot": None,
             "visual_note": "x"},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)
    # _validate_timeline raises ValueError (first beat not 'hook'); _with_retry
    # exhausts and re-raises.
    with pytest.raises(ValueError):
        await c.plan_timeline(
            brief="x", character=_char(), product_present=True,
            target_duration_s=30,
        )


@pytest.mark.asyncio
async def test_with_retry_feeds_last_error_to_feedback_fn(monkeypatch):
    """A callable that accepts one arg receives the previous attempt's
    exception (None first), so it can tell the model exactly what to fix."""
    monkeypatch.setattr("app.gemini.asyncio.sleep", AsyncMock())
    c = GeminiClient(_settings(), _client=_mock_genai_client("{}"))
    seen: list = []
    n = {"c": 0}

    async def fn(last_error=None):
        seen.append(last_error)
        n["c"] += 1
        if n["c"] == 1:
            raise ValueError("boundary broke")
        return "ok"

    assert await c._with_retry(fn, label="t") == "ok"
    assert seen[0] is None
    assert isinstance(seen[1], ValueError) and "boundary broke" in str(seen[1])


@pytest.mark.asyncio
async def test_with_retry_accumulates_distinct_corrections(monkeypatch):
    """When attempts fail for DIFFERENT reasons, the feedback must carry ALL
    prior corrections, not just the last one. Observed live: mid-sentence →
    empty-beats → identical mid-sentence, because the first correction was
    dropped from attempt 3's prompt and the model re-made the same mistake."""
    monkeypatch.setattr("app.gemini.asyncio.sleep", AsyncMock())
    c = GeminiClient(_settings(), _client=_mock_genai_client("{}"))
    seen: list = []
    n = {"c": 0}

    async def fn(last_error=None):
        seen.append(last_error)
        n["c"] += 1
        if n["c"] == 1:
            raise ValueError("beat 2 ends mid-sentence")
        if n["c"] == 2:
            raise ValueError("timeline has no beats")
        return "ok"

    assert await c._with_retry(fn, label="t") == "ok"
    # Attempt 3's feedback must mention BOTH prior rejections.
    assert "beat 2 ends mid-sentence" in str(seen[2])
    assert "timeline has no beats" in str(seen[2])


@pytest.mark.asyncio
async def test_with_retry_still_supports_zero_arg_fn(monkeypatch):
    """Backward compatibility: existing zero-arg _do closures are unchanged."""
    monkeypatch.setattr("app.gemini.asyncio.sleep", AsyncMock())
    c = GeminiClient(_settings(), _client=_mock_genai_client("{}"))
    n = {"c": 0}

    async def fn():
        n["c"] += 1
        if n["c"] == 1:
            raise ValueError("x")
        return "ok"

    assert await c._with_retry(fn, label="t") == "ok"
    assert n["c"] == 2


@pytest.mark.asyncio
async def test_plan_timeline_retry_appends_correction(monkeypatch):
    """When the first sample is REJECTED, the retry prompt must carry the exact
    rejection so the planner fixes THAT beat instead of blind re-rolling (which
    can collapse to an empty timeline)."""
    monkeypatch.setattr("app.gemini.asyncio.sleep", AsyncMock())
    import json
    bad = {
        # Anatomy violation (first beat isn't a hook) — a class the
        # deterministic boundary repair can NOT fix, so it must retry.
        "full_script": "This is genuinely a really good secret. You really must try it today. ",
        "target_duration_s": 30, "hook_type": "result_first",
        "narrative_structure": "testimonial",
        "beats": [
            {"index": 0, "ad_section": "problem",
             "dialogue_text": "This is genuinely a really good secret. ",
             "shot_type": "talking_head", "framing": "selfie",
             "broll_type": None, "broll_shot": None, "visual_note": "talking"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "You really must try it today. ",
             "shot_type": "talking_head", "framing": "tripod",
             "broll_type": None, "broll_shot": None, "visual_note": "cta"},
        ],
    }
    good = {
        "full_script": "This is genuinely a really good secret. You really must try it today. ",
        "target_duration_s": 30, "hook_type": "result_first",
        "narrative_structure": "testimonial",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "This is genuinely a really good secret. ",
             "shot_type": "talking_head", "framing": "selfie",
             "broll_type": None, "broll_shot": None, "visual_note": "talking"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "You really must try it today. ",
             "shot_type": "talking_head", "framing": "tripod",
             "broll_type": None, "broll_shot": None, "visual_note": "cta"},
        ],
    }
    fake = MagicMock()
    fake.aio.models.generate_content = AsyncMock(side_effect=[
        MagicMock(text=json.dumps(bad)),
        MagicMock(text=json.dumps(good)),
    ])
    c = GeminiClient(_settings(), _client=fake)

    res = await c.plan_timeline(
        brief="b", character=_char(), product_present=True,
        target_duration_s=30,
    )
    assert isinstance(res, VideoTimeline)
    calls = fake.aio.models.generate_content.call_args_list
    assert "CORRECTION REQUIRED" not in calls[0].kwargs["contents"]   # 1st clean
    second = calls[1].kwargs["contents"]
    assert "CORRECTION REQUIRED" in second
    assert "first beat must be 'hook'" in second                      # exact reason


def _timeline_with(beat_overrides: dict) -> VideoTimeline:
    """Build a minimal valid 2-beat timeline, applying overrides to beat 0."""
    beat0 = {
        "index": 0, "ad_section": "solution",
        "dialogue_text": "I love this so much",
        "shot_type": "talking_head", "framing": "tripod",
        "broll_type": None, "broll_shot": None,
        "visual_note": "creator talks to camera",
    }
    beat0.update(beat_overrides)
    return VideoTimeline(
        full_script="Okay so I tried this. I love this so much",
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(**beat0),
        ],
    )


def test_validate_rejects_eating_on_talking_head():
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "shot_type": "talking_head", "framing": "tripod",
        "dialogue_text": "I love this so much",
        "visual_note": "creator eating oats while talking",
    })
    with pytest.raises(ValueError, match="mouth-occupying"):
        _validate_timeline(tl)


def test_validate_allows_eating_on_broll():
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "shot_type": "broll", "framing": None,
        "broll_type": "hands_using", "dialogue_text": "I love this so much",
        "broll_shot": "spoon scooping oats, a bite taken", "visual_note": "hands eating",
    })
    _validate_timeline(tl)   # must not raise — eating on B-roll is correct


def test_validate_allows_clean_talking_head():
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "shot_type": "talking_head", "framing": "tripod",
        "dialogue_text": "I love this so much",
        "visual_note": "creator holds the pack up, smiling",
    })
    _validate_timeline(tl)   # holding (not using) the product is allowed


def test_validate_rejects_non_partition_dialogue():
    """Beats whose dialogue_text differ from full_script by an actual WORD (not
    just whitespace) must raise — this is what prevents wrong-word cuts on a
    paid render. A whitespace-only slip is repaired instead (see
    test_validate_repairs_dropped_inter_beat_spaces)."""
    from app.gemini import _validate_timeline
    # full_script (from _timeline_with) is "Okay so I tried this. I love this so
    # much". Tamper beat 0 with a changed WORD ('tried'→'tasted') → genuine
    # content divergence, not repairable.
    tl = _timeline_with({
        "ad_section": "cta", "dialogue_text": "I love this so much",
        "visual_note": "x",
    })
    object.__setattr__(tl.beats[0], "dialogue_text", "Okay so I tasted this. ")
    with pytest.raises(ValueError, match="partition|concatenate"):
        _validate_timeline(tl)


def test_validate_accepts_exact_partition():
    """The happy path: dialogue concatenates to full_script exactly."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "dialogue_text": "I love this so much",
        "visual_note": "creator smiling, holding the pack",
    })
    assert "".join(b.dialogue_text for b in tl.beats) == tl.full_script
    _validate_timeline(tl)   # must not raise


def test_validate_rejects_overlong_beat_words():
    """A SPLITTABLE beat past the ~7-8s ceiling (>16 spoken words at real TTS
    pacing 1.6-2.2 w/s) is a runaway — must be rejected with a split hint. (An
    atomic sentence with no cut point is handled separately — see
    test_validate_allows_unsplittable_verbatim_sentence_over_word_cap.)"""
    from app.gemini import _validate_timeline
    long_beat = ("this beat just keeps going on and on and on. it has far too "
                 "many words to ever fit a single held shot honestly")
    assert len(long_beat.split()) > 16
    script = "Quick hook line right here. " + long_beat
    tl = VideoTimeline(
        full_script=script, target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Quick hook line right here. ",
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talks"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=long_beat,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    with pytest.raises(ValueError, match="spoken words"):
        _validate_timeline(tl)


def test_validate_allows_held_beat_within_ceiling():
    """The director's liberty: a beat in the 5-16 word band (a deliberate
    ~6-7s held shot) is allowed — only runaways past ~8s (>16 words) reject."""
    from app.gemini import _validate_timeline
    held = "this is the one line that genuinely needs a moment to fully land okay"
    assert 5 <= len(held.split()) <= 16
    hook = "Okay so here is the quick hook line. "
    tl = VideoTimeline(
        full_script=hook + held, target_duration_s=20,
        hook_type="result_first", narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text=hook,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talks"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=held,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_beat_word_count_ignores_audio_tags():
    """[sighs] etc. are instruction tokens, not spoken words — they must not
    count toward the per-beat budget."""
    from app.gemini import _validate_timeline
    # 10 spoken words + a tag = still under the 11-word cap.
    beat_text = "[sighs] Girls love intense masculine fragrances like this one right here"
    tl = VideoTimeline(
        full_script="Okay here is the hook. " + beat_text,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay here is the hook. ",
                         shot_type="talking_head", framing="selfie",
                         visual_note="x"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=beat_text,
                         shot_type="talking_head", framing="tripod",
                         visual_note="x"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_rejects_short_talking_head_beat():
    """A talking_head beat under 5 spoken words can't clear Seedance's 2s
    reference-audio minimum at real pacing (~2.0 w/s) — must be rejected."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script="Okay so I tried this. I love this",
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text="I love this",
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    with pytest.raises(ValueError, match="5-word minimum"):
        _validate_timeline(tl)


def test_validate_rejects_trailing_audio_tag():
    """A tag ENDING a beat's dialogue renders audio past the beat's last
    aligned word — it would leak into the NEXT beat's audio slice."""
    from app.gemini import _validate_timeline
    beat_text = "honestly the results are amazing [laughs]"
    tl = VideoTimeline(
        full_script="Okay so I tried this. " + beat_text,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=beat_text,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    with pytest.raises(ValueError, match="ends with an audio tag"):
        _validate_timeline(tl)


def test_validate_allows_leading_audio_tag():
    """A tag OPENING a line (immediately before the words it modifies) is the
    correct placement — must pass."""
    from app.gemini import _validate_timeline
    beat_text = "[laughs] honestly the results are amazing"
    tl = VideoTimeline(
        full_script="Okay so I tried this. " + beat_text,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=beat_text,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_allows_spaced_hyphen_as_dash_boundary():
    """A spaced ASCII hyphen ('...two slots - but...') is a dash used as
    punctuation, not a mid-sentence split — it must be accepted as a legal
    cut edge, exactly like the em-dash. Regression: verbatim scripts that use
    '-' instead of '—' were unsatisfiable and collapsed to 'no beats'."""
    from app.gemini import _validate_timeline
    b0 = "Every good thought can be put in two slots - "
    b1 = "but the best ones always stay with you. "
    tl = VideoTimeline(
        full_script=b0 + b1,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_snap_never_cuts_at_a_mid_word_hyphen():
    """An UNspaced hyphen ('...well-') is a broken compound word, not a dash —
    the repair must NOT keep a cut there; with no legal interior edge the two
    halves merge into one beat."""
    from app.gemini import _validate_timeline
    b0 = "These sneakers honestly changed my whole morning routine. "
    b1 = "This is genuinely a really well-"
    b2 = "known secret, everyone should try today. "
    tl = VideoTimeline(
        full_script=b0 + b1 + b2,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="creator talking"),
            TimelineBeat(index=2, ad_section="cta", dialogue_text=b2,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)
    assert len(tl.beats) == 2
    assert tl.beats[1].dialogue_text == b1 + b2   # never split at 'well-'


def test_snap_repairs_mid_sentence_boundary():
    """A boundary landing mid-sentence is repaired IN CODE — snapped to the
    nearest legal phrase edge — instead of re-rolling the planner. Observed
    live: the planner exhausted all retries re-making the IDENTICAL
    mid-sentence split ('…you can use them for workout or')."""
    from app.gemini import _validate_timeline
    b0 = "These sneakers changed my routine. "
    b1 = "I wear them every single day. You can use them for "
    b2 = "workout or outings. "
    tl = VideoTimeline(
        full_script=b0 + b1 + b2,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="creator talking"),
            TimelineBeat(index=2, ad_section="cta", dialogue_text=b2,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must repair, not raise
    assert len(tl.beats) == 3
    assert tl.beats[1].dialogue_text == "I wear them every single day. "
    assert tl.beats[2].dialogue_text == (
        "You can use them for workout or outings. ")
    # Partition is preserved exactly — VO alignment depends on it.
    assert "".join(b.dialogue_text for b in tl.beats) == tl.full_script


def test_snap_merges_beats_sharing_an_unsplittable_sentence():
    """When two beats straddle ONE sentence with no interior cut point, they
    merge into a single beat: shot design from the member carrying more words,
    ad_section preserving cta-last, indexes renumbered."""
    from app.gemini import _validate_timeline
    b0 = "These sneakers changed my routine. "
    b1 = "You can use them for "
    b2 = "workout or outings. "
    tl = VideoTimeline(
        full_script=b0 + b1 + b2,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="creator gestures at shoes"),
            TimelineBeat(index=2, ad_section="cta", dialogue_text=b2,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must merge, not raise
    assert len(tl.beats) == 2
    merged = tl.beats[1]
    assert merged.dialogue_text == b1 + b2
    assert merged.ad_section == "cta"                 # cta-last preserved
    assert merged.visual_note == "creator gestures at shoes"  # longer member
    assert [b.index for b in tl.beats] == [0, 1]      # renumbered
    assert "".join(b.dialogue_text for b in tl.beats) == tl.full_script


def test_snap_leaves_legal_boundaries_untouched():
    """A well-formed timeline passes through the repair byte-identical."""
    from app.gemini import _validate_timeline
    b0 = "These sneakers honestly changed my whole morning routine. "
    b1 = "Seriously just go try them today. "
    tl = VideoTimeline(
        full_script=b0 + b1,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)
    assert [b.dialogue_text for b in tl.beats] == [b0, b1]
    assert len(tl.beats) == 2


def test_validate_allows_colon_as_phrase_boundary():
    """A beat ending on a colon ('...take note:') is a natural clause boundary
    — a colon introducing a list is a real spoken pause, not a mid-sentence
    split. Regression: a verbatim brief ('If another brand is watching this
    reel, take note:') was unsatisfiable and collapsed the timeline to 'no
    beats' on retry, because ':' was not an accepted cut edge."""
    from app.gemini import _validate_timeline
    b0 = "If another brand is watching this reel, take note: "
    b1 = "it's made with soft french terry cotton that lasts. "
    tl = VideoTimeline(
        full_script=b0 + b1,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_repairs_dropped_inter_beat_spaces():
    """The planner routinely drops the spaces that sit BETWEEN beats when it
    slices full_script (11 beats → ~10 missing boundary spaces). When the beats
    agree with full_script up to whitespace, the validator must re-slice
    full_script onto the beats (so they concatenate EXACTLY) instead of
    rejecting — a spacing slip must not kill the whole plan. Regression: run
    v2-20260713-193418 died on '534 chars vs 544'."""
    from app.gemini import _validate_timeline
    # full_script has proper spacing; the beats dropped the boundary spaces.
    full = ("Skip the fancy deodorant for now. It just ruins your good "
            "shirts. Try this simple thing instead. ")
    b0 = "Skip the fancy deodorant for now."   # dropped trailing space
    b1 = "It just ruins your good shirts."      # dropped trailing space
    b2 = "Try this simple thing instead. "
    assert (b0 + b1 + b2) != full       # genuinely mismatched as emitted
    tl = VideoTimeline(
        full_script=full,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=b1,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=2, ad_section="cta", dialogue_text=b2,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise — repaired in place
    # After repair the beats concatenate to full_script EXACTLY.
    assert "".join(b.dialogue_text for b in tl.beats) == full


def test_validate_rejects_genuine_word_mismatch_with_diff():
    """A real dropped/changed WORD (not just whitespace) is a genuine partition
    error — it must still raise, with a diff that points at the divergence so
    the retry feedback is actionable."""
    from app.gemini import _validate_timeline
    full = "Skip the deodorant. Try this instead. "
    b0 = "Skip the deodorant. "
    b1 = "Try that instead. "     # 'that' != 'this' — real content mismatch
    tl = VideoTimeline(
        full_script=full,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    with pytest.raises(ValueError, match="concatenate to full_script"):
        _validate_timeline(tl)


def _long_atomic_timeline(hook_line: str, *, script_source: str = "verbatim",
                          cta="Go and grab yours right now today. "):
    """A 2-beat timeline whose HOOK is a single (possibly long) sentence."""
    return VideoTimeline(
        full_script=hook_line + cta,
        target_duration_s=30, hook_type="result_first",
        narrative_structure="testimonial", script_source=script_source,
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=hook_line,
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=cta,
                         shot_type="talking_head", framing="tripod", visual_note="cta"),
        ],
    )


def test_validate_allows_unsplittable_verbatim_sentence_over_word_cap():
    """A verbatim sentence with 18 words and NO interior phrase boundary cannot
    be cut ≤16 words without a mid-sentence split — the two rules are mutually
    unsatisfiable, so the soft word cap must yield (the creative team's copy is
    fixed; assembly.split_long_windows chops the visual). Regression: run
    v2-20260713-235113 died looping '18 words' ↔ 'mid-sentence'."""
    from app.gemini import _validate_timeline
    # 18 words, single sentence, no interior . ? ! … — : ; or dash.
    line = ("Bas ek yeh XYXX ki ek hai jo main mostly kahin bhi kabhi bhi "
            "use kar sakta hoon. ")
    assert len(line.split()) == 18
    _validate_timeline(_long_atomic_timeline(line, script_source="verbatim"))


def test_validate_allows_unsplittable_generated_sentence_within_hard_ceiling():
    """A generated atomic sentence a little over the soft cap (but within the
    hard ceiling, 14 as of the 2026-07-31 fast-cut retune) is allowed too —
    there is no legal cut point."""
    from app.gemini import _validate_timeline
    line = ("I have genuinely never tried anything even remotely close "
            "to this before. ")   # 12 words, atomic
    assert len(line.split()) == 12
    _validate_timeline(_long_atomic_timeline(line, script_source="generated"))


def _three_beat_timeline(hook_line, sol_line,
                         cta="Grab yours right now today. "):
    return VideoTimeline(
        full_script=hook_line + sol_line + cta,
        target_duration_s=30, hook_type="result_first",
        narrative_structure="testimonial", script_source="generated",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=hook_line,
                         shot_type="talking_head", framing="selfie",
                         visual_note="x"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=sol_line,
                         shot_type="talking_head", framing="selfie",
                         visual_note="y"),
            TimelineBeat(index=2, ad_section="cta", dialogue_text=cta,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )


def test_validate_allows_exactly_one_held_beat():
    """Fast-cut retune (2026-07-31): beats are capped at 8 words (~2-3s), but
    ONE beat per video may run 9-11 words (~4-5s) — the earned hold. A SECOND
    over-cap beat must be rejected with the split instruction."""
    from app.gemini import _validate_timeline
    held = "I tried this. And honestly it changed my whole routine. "  # 10 w
    short = "It genuinely works every single time. "                   # 6 w
    assert len(held.split()) == 10 and len(short.split()) == 6
    # One held beat → passes.
    _validate_timeline(_three_beat_timeline(held, short))
    # Two over-cap beats → the second is rejected.
    held2 = "Then I used it. And my mornings were never the same. "    # 11 w
    assert len(held2.split()) == 11
    with pytest.raises(ValueError, match="SPLIT"):
        _validate_timeline(_three_beat_timeline(held, held2))


def test_validate_repairs_slow_voice_direction_to_brisk():
    """The video model stretches speech to fill the clip, so a slow-pace
    voice_direction is a computable defect — repaired in code (never re-rolled)
    by substituting 'brisk' for the slow vocabulary."""
    from app.gemini import _validate_timeline
    tl = _three_beat_timeline("Skip the fancy deodorant now. ",
                              "It genuinely works every single time. ")
    tl.beats[0].voice_direction = (
        "In a calm state, with a soft tone and a slow, unhurried pace")
    tl.beats[1].voice_direction = (
        "In an excited state, with a bright tone and a brisk pace")
    _validate_timeline(tl)
    assert "slow" not in tl.beats[0].voice_direction.lower()
    assert "unhurried" not in tl.beats[0].voice_direction.lower()
    assert "brisk" in tl.beats[0].voice_direction
    # An already-brisk direction is left untouched.
    assert tl.beats[1].voice_direction == (
        "In an excited state, with a bright tone and a brisk pace")


def test_validate_enforces_extracted_verbatim_script():
    """When the extractor found a client script, script_source is FORCED to
    verbatim and full_script must equal it in normal form — audio tags,
    parenthetical stage directions and whitespace are ignored; words, case and
    punctuation are not (2026-07-31: the planner's self-classification let
    user scripts get silently paraphrased)."""
    from app.gemini import _validate_timeline
    hook = "Skip the fancy deodorant now. "
    sol = "It genuinely works every single time. "
    cta = "Grab yours right now today. "
    tl = _three_beat_timeline(hook, sol, cta=cta)
    tl.script_source = "generated"   # planner misclassified — must be forced
    # The client's copy: extra whitespace + a stage direction + no audio tags.
    client = f"  {hook}(winks)  {sol}\n{cta} "
    _validate_timeline(tl, expected_script=client)
    assert tl.script_source == "verbatim"
    # A paraphrase ('really' for 'genuinely') must be rejected.
    tl2 = _three_beat_timeline(
        hook, "It really works every single time. ", cta=cta)
    with pytest.raises(ValueError, match="WORD-FOR-WORD"):
        _validate_timeline(tl2, expected_script=client)


def test_script_norm_strips_production_markup():
    """Dev-reported 2026-08-01: the extractor copied 'HOOK VO : \"...\"'
    labels into explicit_script and the raw comparison rejected a CORRECT
    plan at char 0. Labels + wrapping quotes are markup, not spoken words —
    the normal form must ignore them (while never eating a spoken line like
    'OKAY: here's the thing')."""
    from app.gemini import _script_norm
    client_script = ('HOOK VO : "People always ask me how much I spend."\n'
                     'BODY: "It genuinely works."\n'
                     'CTA - "Try it today."')
    spoken = ("People always ask me how much I spend. "
              "It genuinely works. Try it today.")
    assert _script_norm(client_script) == _script_norm(spoken) == spoken
    # A spoken line that merely starts with CAPS+colon is NOT a label.
    assert _script_norm("OKAY: here's the thing") == "OKAY: here's the thing"


def test_validate_verbatim_accepts_labeled_client_script():
    """End-to-end shape of the dev failure: extractor kept 'HOOK VO :'
    labels, planner emitted only spoken words — must PASS now."""
    from app.gemini import _validate_timeline
    hook = "Skip the fancy deodorant now. "
    sol = "It genuinely works every single time. "
    cta = "Grab yours right now today. "
    tl = _three_beat_timeline(hook, sol, cta=cta)
    client = (f'HOOK VO : "{hook.strip()}"\n'
              f'BODY VO : "{sol.strip()}"\n'
              f'CTA : "{cta.strip()}"')
    _validate_timeline(tl, expected_script=client)
    assert tl.script_source == "verbatim"


@pytest.mark.asyncio
async def test_plan_timeline_forced_script_is_sanitized():
    """The CLIENT SCRIPT block handed to the planner must carry only the
    SPOKEN words — echoing 'HOOK VO :' labels into the prompt taught the
    planner to copy them into full_script (breaking the beat partition)."""
    from app.gemini import BriefDirectives
    hook = "Skip the fancy deodorant now. "
    sol = "It genuinely works every single time. "
    cta = "Grab yours right now today. "
    tl = _three_beat_timeline(hook, sol, cta=cta)
    tl.script_source = "verbatim"
    c = GeminiClient(_settings(), _client=object())
    c._generate = AsyncMock(return_value=tl)
    d = BriefDirectives(explicit_script=(
        f'HOOK VO : "{hook.strip()}"\n{sol.strip()}\nCTA : "{cta.strip()}"'))
    await c.plan_timeline(brief="B", character=_motion_char(),
                          product_present=False, directives=d)
    prompt = c._generate.call_args.kwargs["prompt"]
    assert "HOOK VO" not in prompt
    assert "CTA :" not in prompt
    assert hook.strip() in prompt


@pytest.mark.asyncio
async def test_extract_brief_directives_prompts_faithful_extraction(tmp_path):
    """The extractor runs on the fast tier at LOW temperature (fidelity work)
    and its prompt carries the brief plus copy-don't-invent instructions."""
    from app.gemini import BriefDirectives
    c = GeminiClient(_settings(), _client=object())
    c._generate = AsyncMock(return_value=BriefDirectives(
        explicit_script="Say this exactly.",
        visual_directives=["open on the balcony"],
        character_directives=["male, 30s"],
    ))
    out = await c.extract_brief_directives(brief="MY BRIEF TEXT")
    kw = c._generate.call_args.kwargs
    assert kw["tier"] == "fast"
    assert kw["schema"] is BriefDirectives
    assert kw["temperature"] == pytest.approx(0.2)
    assert "MY BRIEF TEXT" in kw["prompt"]
    assert "CHARACTER-FOR-CHARACTER" in kw["prompt"]
    assert out.explicit_script == "Say this exactly."


@pytest.mark.asyncio
async def test_plan_timeline_forces_mode_a_with_extracted_script():
    """With extracted directives, plan_timeline must (a) put the BRIEF at the
    TOP of the prompt, (b) force MODE A with the client script inline, and
    (c) list the visual directives as a checklist."""
    from app.gemini import BriefDirectives
    hook = "Skip the fancy deodorant now. "
    sol = "It genuinely works every single time. "
    cta = "Grab yours right now today. "
    tl = _three_beat_timeline(hook, sol, cta=cta)
    tl.script_source = "verbatim"
    c = GeminiClient(_settings(), _client=object())
    c._generate = AsyncMock(return_value=tl)
    d = BriefDirectives(explicit_script=hook + sol + cta,
                        visual_directives=["open on the balcony at dawn"])
    await c.plan_timeline(brief="THE RAW BRIEF", character=_motion_char(),
                          product_present=False, directives=d)
    prompt = c._generate.call_args.kwargs["prompt"]
    assert "MODE A IS FORCED" in prompt
    assert (hook + sol + cta).strip() in prompt    # client script inline
    assert "open on the balcony at dawn" in prompt  # directive checklist
    # Brief sits at the TOP — before the beat rules, not after them.
    assert prompt.index("THE RAW BRIEF") < prompt.index("### BEAT RULES")


def test_validate_keeps_fine_motor_moments():
    """Fine-motor staging (tying laces etc.) is NOT rejected — the moment
    stays in the ad (user correction 2026-08-01: restage, don't bar). The
    rendering constraint lives in the motion prompt (_FINE_MOTOR_CLAUSE,
    tested in test_pipeline), not in a plan-time ban."""
    from app.gemini import _validate_timeline
    tl = _three_beat_timeline("Skip the fancy deodorant now. ",
                              "It genuinely works every single time. ")
    tl.beats[1].shot_type = "broll"
    tl.beats[1].framing = None
    tl.beats[1].broll_type = "hands_using"
    tl.beats[1].broll_shot = "she is tying the laces of the white sneakers"
    _validate_timeline(tl)   # passes — the moment is kept


def test_validate_repairs_missing_broll_type():
    """A broll beat with broll_type=None fell through every reference branch —
    with shows_creator=False its anchor rendered from TEXT alone (guaranteed
    hallucination). Product-ish text → product_in_scene (gets the product
    ref); otherwise lifestyle."""
    from app.gemini import _validate_timeline
    tl = _three_beat_timeline("Skip the fancy deodorant now. ",
                              "It genuinely works every single time. ")
    tl.beats[1].shot_type = "broll"
    tl.beats[1].framing = None
    tl.beats[1].broll_type = None
    tl.beats[1].broll_shot = "macro of the bottle on the marble counter"
    tl.beats[1].shows_creator = False
    _validate_timeline(tl)
    assert tl.beats[1].broll_type == "product_in_scene"

    tl2 = _three_beat_timeline("Skip the fancy deodorant now. ",
                               "It genuinely works every single time. ")
    tl2.beats[1].shot_type = "broll"
    tl2.beats[1].framing = None
    tl2.beats[1].broll_type = None
    tl2.beats[1].broll_shot = "morning sun through the balcony curtains"
    tl2.beats[1].shows_creator = False
    _validate_timeline(tl2)
    assert tl2.beats[1].broll_type == "lifestyle"


@pytest.mark.asyncio
async def test_describe_product_is_a_grounded_vision_call():
    """describe_product must attach the product photo (image_url) on the fast
    tier at LOW temperature and demand physical facts only."""
    c = GeminiClient(_settings(), _client=object())
    c._generate = AsyncMock(return_value="A matte black plastic pump bottle.")
    out = await c.describe_product(product_ref_url="https://host/p.png")
    kw = c._generate.call_args.kwargs
    assert kw["image_url"] == "https://host/p.png"
    assert kw["tier"] == "fast"
    assert kw["temperature"] == pytest.approx(0.1)
    assert "Physical facts only" in kw["prompt"]
    assert out.startswith("A matte black")


@pytest.mark.asyncio
async def test_plan_timeline_grounds_product_in_vision_description():
    """When a product photo was described, the planner prompt must carry the
    factual description so product shots are staged for the REAL item."""
    hook = "Skip the fancy deodorant now. "
    sol = "It genuinely works every single time. "
    tl = _three_beat_timeline(hook, sol)
    c = GeminiClient(_settings(), _client=object())
    c._generate = AsyncMock(return_value=tl)
    await c.plan_timeline(
        brief="B", character=_motion_char(), product_present=True,
        product_description="A slim white cardboard box with a teal band.",
    )
    prompt = c._generate.call_args.kwargs["prompt"]
    assert "THE ACTUAL PRODUCT" in prompt
    assert "A slim white cardboard box with a teal band." in prompt


def test_validate_swaps_consecutive_duplicate_framing():
    """Framing variety (2026-07-31): the same framing on two CONSECUTIVE
    talking_head beats is auto-swapped to a safe framing (deterministic repair);
    a b-roll cutaway between them breaks adjacency, so a jump-back to the same
    framing is left alone."""
    from app.gemini import _validate_timeline, _VARIETY_SAFE_FRAMINGS
    tl = _three_beat_timeline("Skip the fancy deodorant now. ",
                              "It genuinely works every single time. ")
    for b in tl.beats:
        b.framing = "selfie"
    _validate_timeline(tl)
    framings = [b.framing for b in tl.beats]
    assert framings[0] == "selfie"
    assert framings[1] != "selfie" and framings[1] in _VARIETY_SAFE_FRAMINGS
    # And beat 2 must differ from the (repaired) beat 1.
    assert framings[2] != framings[1]

    # A cutaway between two same-framed talking heads breaks adjacency.
    tl2 = _three_beat_timeline("Skip the fancy deodorant now. ",
                               "It genuinely works every single time. ")
    tl2.beats[0].framing = "selfie"
    tl2.beats[1].shot_type = "broll"
    tl2.beats[1].framing = None
    tl2.beats[1].broll_type = "lifestyle"
    tl2.beats[1].broll_shot = "she laughs with a friend on the sofa"
    tl2.beats[2].framing = "selfie"
    _validate_timeline(tl2)
    assert tl2.beats[2].framing == "selfie"   # jump-back kept


def test_interior_phrase_boundary_edge_cases():
    """The splittability helper must peel a TRAILING boundary (incl. '...' and
    a trailing dash) so an end boundary isn't mistaken for an interior cut,
    while still detecting genuine interior boundaries."""
    from app.gemini import _has_interior_phrase_boundary as h
    # atomic sentences (end boundary only) → no interior boundary
    assert h("one two three four five six") is False
    assert h("one two three four five six...") is False          # trailing ellipsis
    assert h("one two three four five six -") is False           # trailing dash
    assert h("one two three four five six?!") is False
    # genuine interior boundaries → True
    assert h("one two. three four five six") is True
    assert h("wait for it... this changed everything") is True
    assert h("two slots - but the best ones stay") is True
    assert h("first line\nsecond line here") is True


def test_validate_rejects_splittable_beat_over_word_cap():
    """An over-cap beat that DOES contain an interior phrase boundary is
    splittable — it must still be rejected so the planner cuts it there."""
    from app.gemini import _validate_timeline
    # 18 words with an interior sentence end after 'this.' → splittable.
    line = ("I really tried this. And then it completely changed my whole "
            "entire boring morning routine forever now. ")
    assert len(line.split()) > 16
    with pytest.raises(ValueError, match="max 16|SPLIT|split"):
        _validate_timeline(_long_atomic_timeline(line, script_source="generated"))


def test_validate_rejects_runaway_generated_sentence_past_hard_ceiling():
    """A GENERATED atomic sentence past the hard ceiling is genuinely too long
    for one beat — the model can reword, so it's rejected."""
    from app.gemini import _validate_timeline
    line = (" ".join(["word"] * 30) + ". ")   # 30-word atomic runaway
    with pytest.raises(ValueError, match="ceiling|shorten|reword|too long"):
        _validate_timeline(_long_atomic_timeline(line, script_source="generated"))


def test_validate_rejects_dropped_trailing_content():
    """A genuinely dropped trailing SENTENCE (not whitespace) must not be
    silently absorbed into the last beat — it's a real partition gap → raise."""
    from app.gemini import _validate_timeline
    full = ("Skip the fancy deodorant for now. It just ruins your good "
            "shirts. Try this simple thing instead today. ")
    b0 = "Skip the fancy deodorant for now. "
    b1 = "It just ruins your good shirts. "     # last sentence dropped entirely
    tl = VideoTimeline(
        full_script=full,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    with pytest.raises(ValueError, match="concatenate to full_script"):
        _validate_timeline(tl)


def test_validate_allows_solution_proof_interleaving():
    """Real UGC ads interleave feature/solution beats with proof beats
    (feature → proof → feature → proof). solution and proof share a body rank;
    only hook-first, problem-before-body, and cta-last-contiguous are enforced.
    Regression: run v2-20260713-193418 rejected
    ['hook','solution','proof','solution','proof','cta']."""
    from app.gemini import _validate_timeline
    parts = ["Okay so listen up now. ", "It is soft french terry. ",
             "Look how well it holds. ", "The fit is relaxed but sharp. ",
             "Seriously it lasts for years. ", "Go grab yours today now. "]
    tl = VideoTimeline(
        full_script="".join(parts),
        target_duration_s=30, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=parts[0],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=parts[1],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=2, ad_section="proof", dialogue_text=parts[2],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=3, ad_section="solution", dialogue_text=parts[3],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=4, ad_section="proof", dialogue_text=parts[4],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=5, ad_section="cta", dialogue_text=parts[5],
                         shot_type="talking_head", framing="tripod", visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_still_rejects_problem_after_body():
    """Loosening solution↔proof must NOT let a 'problem' beat come after the
    body — hook → problem → {solution/proof} → cta order still holds there."""
    from app.gemini import _validate_timeline
    parts = ["Okay so listen up now. ", "It is soft french terry here. ",
             "But my old ones always ripped. ", "Go grab yours today now. "]
    tl = VideoTimeline(
        full_script="".join(parts),
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=parts[0],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text=parts[1],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=2, ad_section="problem", dialogue_text=parts[2],
                         shot_type="talking_head", framing="selfie", visual_note="x"),
            TimelineBeat(index=3, ad_section="cta", dialogue_text=parts[3],
                         shot_type="talking_head", framing="tripod", visual_note="cta"),
        ],
    )
    with pytest.raises(ValueError, match="out of order"):
        _validate_timeline(tl)


def test_validate_allows_semicolon_as_phrase_boundary():
    """A semicolon separates independent clauses with a real spoken pause, so
    a cut may land there — same clause-boundary category as the colon."""
    from app.gemini import _validate_timeline
    b0 = "I tried every sweatpant out there; "
    b1 = "none of them held their shape after a wash. "
    tl = VideoTimeline(
        full_script=b0 + b1,
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook", dialogue_text=b0,
                         shot_type="talking_head", framing="selfie",
                         visual_note="creator talking"),
            TimelineBeat(index=1, ad_section="cta", dialogue_text=b1,
                         shot_type="talking_head", framing="tripod",
                         visual_note="cta"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_allows_short_or_empty_dialogue_on_broll():
    """The 5-word floor is a talking_head (lip-sync reference audio) rule —
    broll beats carry VO or silence, so short/empty dialogue is fine."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script="Okay so I tried this. so good. ",
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text="so good. ",
                         shot_type="broll", broll_type="hands_using",
                         broll_shot="spoon scooping oats",
                         visual_note="hands with the bowl"),
            TimelineBeat(index=2, ad_section="cta", dialogue_text="",
                         shot_type="broll", broll_type="product_closeup",
                         broll_shot="pack resting on the counter",
                         visual_note="the pack"),
        ],
    )
    _validate_timeline(tl)   # must not raise


def test_validate_accepts_phrase_aligned_boundaries():
    """Boundaries that land on terminal punctuation (.) and an em-dash (—) are
    natural breaks — the timeline must pass."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script=("Men, if you're trying to impress a woman — "
                     "here's a small tip. Skip the deodorant."),
        target_duration_s=20, hook_type="controversial",
        narrative_structure="listicle",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Men, if you're trying to impress a woman — ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="problem",
                         dialogue_text="here's a small tip. ",
                         shot_type="broll", broll_type="product_closeup",
                         broll_shot="the pack on the dresser",
                         visual_note="product on the dresser"),
            TimelineBeat(index=2, ad_section="cta",
                         dialogue_text="Skip the deodorant.",
                         shot_type="broll", broll_type="product_closeup",
                         broll_shot="the pack held up",
                         visual_note="hand holds the pack"),
        ],
    )
    _validate_timeline(tl)   # must not raise — boundaries on — and .


def test_validate_repairs_mid_sentence_split():
    """A boundary that lands mid-clause ('...trying to' | 'impress a woman')
    splits one sentence across two beats — the repair merges the halves into
    the talking_head beat (the member with more spoken words)."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script="Men, if you're trying to impress a woman. Just try it.",
        target_duration_s=20, hook_type="controversial",
        narrative_structure="listicle",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Men, if you're trying to ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="problem",
                         dialogue_text="impress a woman. ",
                         shot_type="broll", broll_type="product_closeup",
                         broll_shot="the pack on the dresser",
                         visual_note="product on the dresser"),
            TimelineBeat(index=2, ad_section="cta",
                         dialogue_text="Just try it.",
                         shot_type="broll", broll_type="product_closeup",
                         broll_shot="the pack held up",
                         visual_note="hand holds the pack"),
        ],
    )
    _validate_timeline(tl)   # must repair, not raise
    assert len(tl.beats) == 2
    assert tl.beats[0].dialogue_text == (
        "Men, if you're trying to impress a woman. ")
    assert tl.beats[0].shot_type == "talking_head"   # longer member kept
    assert tl.beats[0].ad_section == "hook"
    assert "".join(b.dialogue_text for b in tl.beats) == tl.full_script


def test_validate_accepts_newline_terminated_verbatim_lines():
    """A verbatim script whose lines break at newlines (no terminal
    punctuation) cuts naturally at the line break — must pass."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script="here's a small tip\nSkip the deodorant. Wear a good perfume\n",
        target_duration_s=20, hook_type="curiosity_gap",
        narrative_structure="listicle", script_source="verbatim",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="here's a small tip\n",
                         shot_type="broll", broll_type="product_closeup",
                         broll_shot="the pack on the dresser",
                         visual_note="product on the dresser"),
            TimelineBeat(index=1, ad_section="cta",
                         dialogue_text="Skip the deodorant. Wear a good perfume\n",
                         shot_type="talking_head", framing="tripod",
                         visual_note="creator talks to camera"),
        ],
    )
    _validate_timeline(tl)   # must not raise — boundary at \n


def test_validate_accepts_last_beat_without_terminal_punctuation():
    """The last beat ends the script — it is exempt from the phrase-boundary
    rule even with no terminal punctuation."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script="I really love this stuff so much. it just works every time",
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="I really love this stuff so much. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="cta",
                         dialogue_text="it just works every time",
                         shot_type="talking_head", framing="tripod",
                         visual_note="creator talks to camera"),
        ],
    )
    _validate_timeline(tl)   # must not raise — last beat exempt


def test_validate_allows_reaction_beat_and_excludes_from_concatenation():
    """A reaction beat (empty dialogue + hold_s>0) is a silent screen-time hold:
    it must pass validation AND be excluded from the script-partition check."""
    from app.gemini import _validate_timeline
    tl = VideoTimeline(
        full_script="Okay so I tried this. It actually really works so well.",
        target_duration_s=20, hook_type="result_first",
        narrative_structure="testimonial",
        beats=[
            TimelineBeat(index=0, ad_section="hook",
                         dialogue_text="Okay so I tried this. ",
                         shot_type="talking_head", framing="lav_mic",
                         visual_note="creator with mic, talking"),
            TimelineBeat(index=1, ad_section="solution", dialogue_text="",
                         beat_kind="reaction", hold_s=0.8,
                         shot_type="broll", broll_type="product_in_scene",
                         broll_shot="she closes her eyes, soaks it in",
                         shows_creator=True,
                         visual_note="creator pauses, eyes closed, soaking it in"),
            TimelineBeat(index=2, ad_section="cta",
                         dialogue_text="It actually really works so well.",
                         shot_type="talking_head", framing="tripod",
                         visual_note="creator talks to camera"),
        ],
    )
    # The reaction beat contributes "" — speech beats still partition the script.
    _validate_timeline(tl)   # must not raise


def test_validate_rejects_reaction_beat_without_hold():
    """A reaction beat with no positive hold_s is malformed — must raise."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "dialogue_text": "I love this so much",
        "visual_note": "creator talks to camera",
    })
    # Tamper beat 0 into a reaction with empty dialogue but no hold.
    object.__setattr__(tl.beats[0], "beat_kind", "reaction")
    object.__setattr__(tl.beats[0], "dialogue_text", "")
    object.__setattr__(tl, "full_script", "I love this so much")
    with pytest.raises(ValueError, match="hold_s"):
        _validate_timeline(tl)


def test_validate_rejects_face_accessory_on_talking_head():
    """A talking_head whose face is hidden by sunglasses/a hat brim breaks the
    lipsync (the face must be visible) — must raise."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "shot_type": "talking_head", "framing": "tripod",
        "dialogue_text": "I love this so much",
        "visual_note": "creator talks, sunglasses over her eyes",
    })
    with pytest.raises(ValueError, match="obscured|face"):
        _validate_timeline(tl)


def test_validate_rejects_illogical_hands_staging():
    """Physically-illogical hand staging (caressing / extra / phantom hands)
    produces phantom-hand artifacts — must raise."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta", "shot_type": "broll", "framing": None,
        "broll_type": "product_closeup", "broll_shot": "two pairs of hands caress the pack",
        "dialogue_text": "I love this so much",
        "visual_note": "multiple hands caress the product",
    })
    with pytest.raises(ValueError, match="illogical|hands"):
        _validate_timeline(tl)


@pytest.mark.asyncio
async def test_plan_timeline_prompt_has_character_and_product_context():
    """The scene-first director must see the cast creator's look + setting and
    the product-fidelity guidance in its prompt."""
    tl = {
        "full_script": "Okay so I tried this. And honestly it works.",
        "target_duration_s": 20, "hook_type": "result_first",
        "narrative_structure": "testimonial", "script_source": "generated",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "Okay so I tried this. ",
             "shot_type": "talking_head", "framing": "lav_mic",
             "broll_type": None, "broll_shot": None,
             "visual_note": "creator with mic", "beat_kind": "speech",
             "hold_s": None, "shows_creator": True},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "And honestly it works.",
             "shot_type": "broll", "framing": None,
             "broll_type": "product_closeup", "broll_shot": "the pack",
             "visual_note": "product close-up", "beat_kind": "speech",
             "hold_s": None, "shows_creator": False},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)
    char = _char(wardrobe="fitted olive linen shirt",
                 setting="apartment_balcony_natural_light")
    await c.plan_timeline(
        brief="a skincare serum", character=char, product_present=True,
        target_duration_s=20,
    )
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    # The cast creator's concrete look + setting reaches the director.
    assert "fitted olive linen shirt" in contents
    assert "apartment_balcony_natural_light" in contents
    assert "THE CREATOR" in contents
    # Product-fidelity guidance is present when a product reference exists.
    assert "PRODUCT FIDELITY" in contents
    # The scene-first shot-design fields are explained.
    assert "shows_creator" in contents and "beat_kind" in contents
    # Brief directives are framed as mandatory (so a brief-flagged important
    # shot gets a real visible beat, not dropped or shrunk to a silent hold).
    assert "BRIEF DIRECTIVES" in contents and "MANDATORY" in contents


@pytest.mark.asyncio
async def test_plan_timeline_no_product_branch():
    """With no product reference the director gets the NO-PRODUCT guidance, not
    the fidelity block."""
    tl = {
        "full_script": "Okay so I tried this. And honestly it really works well.",
        "target_duration_s": 20, "hook_type": "result_first",
        "narrative_structure": "testimonial", "script_source": "generated",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "Okay so I tried this. ",
             "shot_type": "talking_head", "framing": "lav_mic",
             "broll_type": None, "broll_shot": None,
             "visual_note": "creator with mic"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "And honestly it really works well.",
             "shot_type": "talking_head", "framing": "tripod",
             "broll_type": None, "broll_shot": None,
             "visual_note": "creator talks"},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)
    await c.plan_timeline(
        brief="a motivational reel", character=_char(), product_present=False,
        target_duration_s=20,
    )
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    assert "NO PRODUCT REFERENCE" in contents
    assert "PRODUCT FIDELITY" not in contents


@pytest.mark.asyncio
async def test_plan_timeline_prompt_has_verbatim_mode_and_brief():
    """A brief containing an explicit script must reach the planner with MODE A
    verbatim instructions — so the script is preserved, not paraphrased."""
    tl = {
        "full_script": "Stop. Wear a good perfume. Heads will turn.",
        "target_duration_s": 12, "hook_type": "callout",
        "narrative_structure": "pas", "script_source": "verbatim",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "Stop. Wear a good perfume. ",
             "shot_type": "talking_head", "framing": "lav_mic",
             "broll_type": None, "broll_shot": None, "visual_note": "x"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "Heads will turn.",
             "shot_type": "broll", "framing": None,
             "broll_type": "product_closeup",
             "broll_shot": "the bottle on the dresser", "visual_note": "y"},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)
    res = await c.plan_timeline(
        brief="SCRIPT: Stop. Wear a good perfume. Heads will turn.",
        character=_char(), product_present=True,
        target_duration_s=None,
    )
    assert res.script_source == "verbatim"
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    assert "MODE A" in contents and "WORD-FOR-WORD" in contents
    # AI-decided duration (no target) → the 15-45s self-choose band is present.
    assert "between 15 and 45" in contents
    assert "SCRIPT: Stop. Wear a good perfume" in contents   # brief passed through


@pytest.mark.asyncio
async def test_plan_timeline_forced_target_shows_word_budget():
    tl = {
        "full_script": "Okay you have to try this. Grab yours on the site.",
        "target_duration_s": 20,
        "hook_type": "question", "narrative_structure": "pas",
        "script_source": "generated",
        "beats": [
            {"index": 0, "ad_section": "hook",
             "dialogue_text": "Okay you have to try this. ",
             "shot_type": "talking_head", "framing": "selfie",
             "broll_type": None, "broll_shot": None, "visual_note": "x"},
            {"index": 1, "ad_section": "cta",
             "dialogue_text": "Grab yours on the site.",
             "shot_type": "talking_head", "framing": "tripod",
             "broll_type": None, "broll_shot": None, "visual_note": "y"},
        ],
    }
    import json
    fake = _mock_genai_client(json.dumps(tl))
    c = GeminiClient(_settings(), _client=fake)
    await c.plan_timeline(
        brief="rough idea", character=_char(), product_present=True,
        target_duration_s=20,
    )
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    assert "HARD WORD BUDGET" in contents and "between 15 and 45" not in contents
    # Phrase-boundary guidance: cut only at natural phrase edges.
    assert "natural phrase boundaries" in contents
    assert "NEVER split a single sentence across two beats" in contents


def test_validate_skips_word_budget_for_verbatim():
    """A verbatim script over the budget must NOT be rejected — its length is
    the creative team's call."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({"ad_section": "cta",
                         "dialogue_text": "I love this so much",
                         "visual_note": "x"})
    object.__setattr__(tl, "script_source", "verbatim")
    _validate_timeline(tl, max_words=3)      # way under 10 words — but verbatim, so OK


def test_validate_rejects_over_budget_total_script():
    """The whole script must fit the duration's word budget — a 77-word script
    for a 30s target produced a 43s VO on a real run."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({"ad_section": "cta",
                         "dialogue_text": "I love this so much",
                         "visual_note": "x"})
    _validate_timeline(tl, max_words=10)     # exactly at the cap → fine
    with pytest.raises(ValueError, match="word budget"):
        _validate_timeline(tl, max_words=9)  # one under → rejected


def test_framing_camera_hint_covers_all_framings():
    """Every TalkingFraming the planner can emit must have a camera hint. The
    hints describe the camera POV, NOT a visible rig — only the mirror framing
    intentionally shows the phone (in the reflection); no other hint may name a
    recording device, or the image model renders a tripod/phone in frame."""
    import typing
    from app.gemini import _FRAMING_CAMERA_HINT, TalkingFraming

    framings = set(typing.get_args(TalkingFraming))
    assert framings == set(_FRAMING_CAMERA_HINT), "every framing needs a hint"
    assert "reflection" in _FRAMING_CAMERA_HINT["mirror"]   # phone-in-mirror is intentional
    for f, hint in _FRAMING_CAMERA_HINT.items():
        if f == "mirror":
            continue
        low = hint.lower()
        for rig in ("phone", "tripod", "gimbal", "ring light", "selfie stick"):
            assert rig not in low, f"{f} hint must not name a visible rig ({rig})"


@pytest.mark.asyncio
async def test_write_first_frame_prompt_uses_correct_camera_hint(tmp_path):
    """A framing must get its hint AND the no-recording-device rule, so the
    image model never renders the phone/tripod rig in frame."""
    skill_path = tmp_path / "character-image-skill.md"
    skill_path.write_text("rules")
    fake = _mock_genai_client("prompt")
    c = GeminiClient(_settings(), _client=fake, image_skill_md_path=skill_path)
    char = CharacterDescription(
        age=25, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="messy_high_bun",
        setting="apartment_kitchen_counter", expression="candid_mid_laugh",
        hair="x", face="y", wardrobe="z", energy="w", voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )
    await c.write_first_frame_prompt(
        brief="b", character=char, opening_chunk_action="a",
        framing="lav_mic", product_in_first_frame=False, product_ref_url=None,
    )
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    assert "lav_mic" in contents
    # The no-rig rule must be present so the recording device stays out of frame.
    assert "NEVER render the recording device" in contents
    assert "tripod" in contents          # named in the ban list
    assert "back camera" not in contents


def test_client_requires_api_key():
    s = Settings(evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
                 aws_bucket_name="b", aws_region="r", gemini_api_key="",
                 elevenlabs_api_key="el", sync_api_key="sy",
                 llm_provider="gemini")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiClient(s)


@pytest.mark.asyncio
async def test_generate_script_returns_parsed_draft():
    payload = '{"full_script": "hi", "tone_notes": "casual", "n_chunks": 1}'
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)

    draft = await c.generate_script(brief="ad for oats", duration_s=12)

    assert isinstance(draft, ScriptDraft)
    assert draft.full_script == "hi"
    assert draft.n_chunks == 1
    call_kwargs = fake.aio.models.generate_content.call_args.kwargs
    assert call_kwargs["model"] == GEMINI_FAST_MODEL
    assert "ad for oats" in call_kwargs["contents"]
    assert "12" in call_kwargs["contents"]
    assert call_kwargs["config"].response_schema is ScriptDraft


@pytest.mark.asyncio
async def test_generate_script_computes_n_chunks_for_long_durations():
    # Phase 2 of the v2.4 overhaul: chunks are now 8s (was 15s).
    # 42s duration → ceil(42 / 8) = 6 chunks.
    payload = '{"full_script": "x", "tone_notes": "t", "n_chunks": 6}'
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    draft = await c.generate_script(brief="b", duration_s=42)
    kw = fake.aio.models.generate_content.call_args.kwargs
    assert "42 seconds" in kw["contents"]
    assert "6 chunks of 8s each" in kw["contents"]


def _example_screenplay(n: int = 2) -> Screenplay:
    return Screenplay(
        overall_arc="open hook then product reveal",
        scene_beats=[
            SceneBeat(
                index=i, visual_action=f"beat {i}",
                framing="selfie" if i == 0 else "counter",
                camera_intent="static" if i == 0 else "slow push-in",
            )
            for i in range(n)
        ],
    )


@pytest.mark.asyncio
async def test_write_screenplay_returns_parsed_struct():
    payload = (
        '{"overall_arc":"hook then reveal",'
        '"scene_beats":['
        '{"index":0,"visual_action":"selfie hook","framing":"selfie",'
        '"camera_intent":"static"},'
        '{"index":1,"visual_action":"over-counter reveal","framing":"counter",'
        '"camera_intent":"slow push-in"}]}'
    )
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    draft = ScriptDraft(full_script="hi. bye.", tone_notes="t", n_chunks=2)
    sp = await c.write_screenplay(brief="ad for X", script=draft)
    assert isinstance(sp, Screenplay)
    assert len(sp.scene_beats) == 2
    assert sp.scene_beats[0].framing == "selfie"
    assert sp.scene_beats[1].framing == "counter"
    kw = fake.aio.models.generate_content.call_args.kwargs
    assert kw["model"] == GEMINI_FAST_MODEL
    assert kw["config"].response_schema is Screenplay


@pytest.mark.asyncio
async def test_split_script_returns_chunks():
    payload = (
        '[{"index":0,"t_start":0,"t_end":15,"dialogue":"hi","action":"smile","summary":"intro","framing":"selfie"},'
        ' {"index":1,"t_start":15,"t_end":30,"dialogue":"bye","action":"wave","summary":"outro","framing":"counter"}]'
    )
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    draft = ScriptDraft(full_script="hi. bye.", tone_notes="t", n_chunks=2)
    sp = _example_screenplay(n=2)
    chunks = await c.split_script(
        brief="ad for X", script=draft, screenplay=sp, n_chunks=2,
    )
    assert len(chunks) == 2
    assert chunks[0].dialogue == "hi"
    assert chunks[0].framing == "selfie"
    assert chunks[1].framing == "counter"
    assert chunks[1].summary == "outro"
    kw = fake.aio.models.generate_content.call_args.kwargs
    assert kw["model"] == GEMINI_FAST_MODEL
    # The screenplay beats and the brief must reach the model.
    assert "ad for X" in kw["contents"]
    assert "selfie" in kw["contents"] and "counter" in kw["contents"]


@pytest.mark.asyncio
async def test_describe_character_returns_struct():
    payload = (
        '{"age":24,"gender":"female","ethnicity":"Indian",'
        '"region":"north_indian","skin_tone":"fair_golden",'
        '"hair_style":"long_loose_natural_waves",'
        '"setting":"apartment_kitchen_counter",'
        '"expression":"candid_mid_laugh",'
        '"hair":"curly","face":"smooth","wardrobe":"tee","energy":"warm",'
        '"voice_vibe":"upbeat_lively",'
        '"voice_persona":{"gender":"female","age_band":"mid-20s",'
        '"timbre":"warm bright","pace":"calm conversational","warmth":"friendly"}}'
    )
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    char = await c.describe_character(brief="oats ad")
    assert char.age == 24 and char.ethnicity == "Indian"
    # Casting runs from the BRIEF only — no script in the prompt.
    contents = fake.aio.models.generate_content.call_args.kwargs["contents"]
    assert "oats ad" in contents
    assert "SCRIPT:" not in contents




from pathlib import Path


@pytest.mark.asyncio
async def test_write_seedance_prompt_uses_pro_model_and_skill_md(tmp_path):
    skill_md = "DUMMY SKILL CONTENT (instructions about Seedance prompting)"
    skill_path = tmp_path / "skill.md"
    skill_path.write_text(skill_md)

    fake = _mock_genai_client("the optimized seedance prompt goes here.")
    c = GeminiClient(_settings(), _client=fake, skill_md_path=skill_path)

    chunk = ChunkSpec(
        index=0, t_start=0, t_end=15, dialogue="hi",
        action="raises bowl", summary="intro shot", framing="selfie",
    )
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian",
        region="north_indian", skin_tone="fair_golden",
        hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter",
        expression="candid_mid_laugh",
        hair="wavy", face="smooth", wardrobe="tee", energy="warm",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )

    out = await c.write_seedance_prompt(
        chunk=chunk, character=char, prev_summary=None,
    )

    assert out == "the optimized seedance prompt goes here."

    kw = fake.aio.models.generate_content.call_args.kwargs
    assert kw["model"] == GEMINI_PRO_MODEL
    assert kw["config"].system_instruction.startswith("DUMMY SKILL CONTENT")
    assert kw["config"].response_schema is None


@pytest.mark.asyncio
async def test_should_open_with_product_true():
    payload = '{"in_first_frame": true, "reasoning": "product-forward demo"}'
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    out = await c.should_open_with_product(
        brief="product demo for X", opening_chunk_action="holds X up to camera",
    )
    assert out is True
    # Verify structured-output schema was used.
    kw = fake.aio.models.generate_content.call_args.kwargs
    from app.gemini import ProductInFirstFrameDecision
    assert kw["config"].response_schema is ProductInFirstFrameDecision


@pytest.mark.asyncio
async def test_should_open_with_product_false():
    payload = '{"in_first_frame": false, "reasoning": "story-led hook first"}'
    fake = _mock_genai_client(payload)
    c = GeminiClient(_settings(), _client=fake)
    out = await c.should_open_with_product(
        brief="story-led ad", opening_chunk_action="greets the camera",
    )
    assert out is False


@pytest.mark.asyncio
async def test_should_open_with_product_falls_back_to_false_on_parse_error():
    """Garbage response → safe default of False (story-led opening)."""
    fake = _mock_genai_client("¯\\_(ツ)_/¯")
    c = GeminiClient(_settings(), _client=fake)
    out = await c.should_open_with_product(
        brief="b", opening_chunk_action="a",
    )
    assert out is False


@pytest.mark.asyncio
async def test_write_seedance_prompt_includes_prev_summary_when_provided(tmp_path):
    skill_path = tmp_path / "skill.md"; skill_path.write_text("x")
    fake = _mock_genai_client("prompt")
    c = GeminiClient(_settings(), _client=fake, skill_md_path=skill_path)

    chunk = ChunkSpec(index=1, t_start=15, t_end=30, dialogue="d",
                      action="a", summary="s", framing="counter")
    char = CharacterDescription(
        age=24, gender="female", ethnicity="Indian",
        region="north_indian", skin_tone="fair_golden",
        hair_style="messy_high_bun",
        setting="apartment_kitchen_counter",
        expression="candid_mid_laugh",
        hair="x", face="y", wardrobe="z", energy="w",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )

    await c.write_seedance_prompt(
        chunk=chunk, character=char,
        prev_summary="she just picked up the bowl",
    )

    kw = fake.aio.models.generate_content.call_args.kwargs
    assert "she just picked up the bowl" in kw["contents"]
    assert "counter" in kw["contents"]


@pytest.mark.asyncio
async def test_write_character_prompt_uses_image_skill_md(tmp_path):
    image_skill_md = "DUMMY IMAGE SKILL CONTENT — anti-AI rules"
    skill_path = tmp_path / "character-image-skill.md"
    skill_path.write_text(image_skill_md)

    fake = _mock_genai_client("a character prompt string")
    c = GeminiClient(_settings(), _client=fake, image_skill_md_path=skill_path)

    char = CharacterDescription(
        age=25, gender="female", ethnicity="Indian",
        region="north_indian", skin_tone="fair_golden",
        hair_style="messy_high_bun",
        setting="apartment_kitchen_counter",
        expression="candid_mid_laugh",
        hair="messy bun", face="warm honey-brown skin",
        wardrobe="ribbed olive tank", energy="warm wellness energy",
        voice_vibe="warm_relatable",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )

    out = await c.write_character_prompt(
        brief="health oats brand", character=char,
    )

    assert out == "a character prompt string"
    kw = fake.aio.models.generate_content.call_args.kwargs
    assert kw["model"] == GEMINI_FAST_MODEL
    assert kw["config"].system_instruction.startswith("DUMMY IMAGE SKILL")
    assert kw["config"].response_schema is None
    assert "health oats brand" in kw["contents"]
    assert "messy bun" in kw["contents"]
    assert "warm honey-brown skin" in kw["contents"]

def test_skill_md_loader_reads_utf8(tmp_path):
    skill_path = tmp_path / "skill.md"
    skill_path.write_bytes("start А end".encode("utf-8"))

    c = GeminiClient(
        _settings(),
        _client=_mock_genai_client("ok"),
        skill_md_path=skill_path,
    )

    assert c._load_skill_md() == "start А end"

@pytest.mark.asyncio
async def test_write_first_frame_prompt_includes_action_and_product(tmp_path):
    skill_path = tmp_path / "character-image-skill.md"
    skill_path.write_text("rules")
    fake = _mock_genai_client("a first frame prompt")
    c = GeminiClient(_settings(), _client=fake, image_skill_md_path=skill_path)

    char = CharacterDescription(
        age=25, gender="female", ethnicity="Indian",
        region="north_indian", skin_tone="fair_golden",
        hair_style="messy_high_bun",
        setting="apartment_kitchen_counter",
        expression="candid_mid_laugh",
        hair="x", face="y", wardrobe="z", energy="w",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )

    out = await c.write_first_frame_prompt(
        brief="oats brand",
        character=char,
        opening_chunk_action="she's mid-laugh holding a bowl of oats",
        framing="counter",
        product_in_first_frame=True,
        product_ref_url="https://prod/oats.jpg",
    )

    assert out == "a first frame prompt"
    kw = fake.aio.models.generate_content.call_args.kwargs
    assert "she's mid-laugh holding a bowl of oats" in kw["contents"]
    assert "counter" in kw["contents"]
    assert "https://prod/oats.jpg" in kw["contents"]
    assert "PRODUCT IN FIRST FRAME: YES" in kw["contents"]


@pytest.mark.asyncio
async def test_write_first_frame_prompt_no_product_branch(tmp_path):
    skill_path = tmp_path / "character-image-skill.md"
    skill_path.write_text("rules")
    fake = _mock_genai_client("prompt")
    c = GeminiClient(_settings(), _client=fake, image_skill_md_path=skill_path)

    char = CharacterDescription(
        age=25, gender="female", ethnicity="Indian",
        region="north_indian", skin_tone="fair_golden",
        hair_style="messy_high_bun",
        setting="apartment_kitchen_counter",
        expression="candid_mid_laugh",
        hair="x", face="y", wardrobe="z", energy="w",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )

    await c.write_first_frame_prompt(
        brief="b", character=char,
        opening_chunk_action="a", framing="selfie",
        product_in_first_frame=False,
        product_ref_url=None,
    )

    kw = fake.aio.models.generate_content.call_args.kwargs
    assert "PRODUCT IN FIRST FRAME: NO" in kw["contents"]


# ---------- Task 2: image_url threaded through _generate ----------

@pytest.mark.asyncio
async def test_generate_forwards_image_url_to_claude_backend():
    from app.config import Settings
    s = Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="",
        elevenlabs_api_key="el", sync_api_key="sy",
        anthropic_api_key="ant-test", llm_provider="claude",
    )
    c = GeminiClient(s, _claude_client=object())   # provider=claude
    c._claude = AsyncMock()
    c._claude.generate = AsyncMock(return_value="ok")
    out = await c._generate(
        tier="pro", prompt="p", system_instruction="sys",
        image_url="https://host/a.png",
    )
    assert out == "ok"
    assert c._claude.generate.call_args.kwargs["image_url"] == \
        "https://host/a.png"


@pytest.mark.asyncio
async def test_generate_forwards_all_args_to_claude_backend():
    """The whole seam round-trips: tier/schema/system/temperature/image_url all
    reach the claude backend unmangled (not just image_url)."""
    from app.config import Settings
    s = Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="",
        elevenlabs_api_key="el", sync_api_key="sy",
        anthropic_api_key="ant-test", llm_provider="claude",
    )
    c = GeminiClient(s, _claude_client=object())
    c._claude = AsyncMock()
    c._claude.generate = AsyncMock(return_value="ok")
    await c._generate(
        tier="fast", prompt="body", schema=ScriptDraft,
        system_instruction="sys", temperature=0.55,
        image_url="https://host/x.png",
    )
    kw = c._claude.generate.call_args.kwargs
    assert kw["tier"] == "fast"
    assert kw["prompt"] == "body"
    assert kw["schema"] is ScriptDraft
    assert kw["system_instruction"] == "sys"
    assert kw["temperature"] == pytest.approx(0.55)
    assert kw["image_url"] == "https://host/x.png"


@pytest.mark.asyncio
async def test_generate_image_url_defaults_to_none_on_claude_backend():
    """Existing text-only callers (no image_url) must still forward None — the
    backend then keeps the plain-string user turn."""
    from app.config import Settings
    s = Settings(
        evolink_api_key="evo", aws_access_key_id="", aws_secret_access_key="",
        aws_bucket_name="b", aws_region="r", gemini_api_key="",
        elevenlabs_api_key="el", sync_api_key="sy",
        anthropic_api_key="ant-test", llm_provider="claude",
    )
    c = GeminiClient(s, _claude_client=object())
    c._claude = AsyncMock()
    c._claude.generate = AsyncMock(return_value="ok")
    await c._generate(tier="lite", prompt="p")
    assert c._claude.generate.call_args.kwargs["image_url"] is None


# ---------- Task 3: write_motion_prompt ----------

def _motion_char(**over) -> CharacterDescription:
    fields = dict(
        age=24, gender="female", ethnicity="Indian", region="north_indian",
        skin_tone="fair_golden", hair_style="long_loose_natural_waves",
        setting="apartment_kitchen_counter", expression="candid_mid_laugh",
        hair="wavy", face="smooth", wardrobe="tee", energy="warm",
        voice_vibe="upbeat_lively",
        voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                                   timbre="warm bright", pace="calm conversational",
                                   warmth="friendly"),
    )
    fields.update(over)
    return CharacterDescription(**fields)


@pytest.mark.asyncio
async def test_write_motion_prompt_sync_talk_uses_skill_image_and_no_line(tmp_path):
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL RULES")
    c = GeminiClient(_settings(), _client=object(),
                     skill_md_path=skill_path)   # _generate is mocked below,
    # so the provider is irrelevant; _client=object() avoids building genai.
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="tight prompt", restaged=False))

    beat = SimpleNamespace(
        index=2, ad_section="solution", shot_type="talking_head",
        framing="selfie", broll_type=None, broll_shot=None,
        dialogue_text="this changed my mornings", visual_note="she grins",
        beat_kind="speech")
    char = _motion_char()

    out = await c.write_motion_prompt(
        beat=beat, character=char, window_len=3.5, mode="sync_talk",
        uses_product=False, anchor_url="https://host/a2.png",
        timeline_overview="beat 2 [solution/talking_head/speech]: ...",
        fallback_example="BASELINE FSTRING")
    assert out.prompt == "tight prompt" and out.restaged is False

    kw = c._generate.call_args.kwargs
    assert kw["tier"] == "pro"
    assert kw["schema"] is MotionPrompt
    assert kw["image_url"] == "https://host/a2.png"
    # Two cached blocks: skill-MD prefix (shared with write_seedance_prompt),
    # timeline overview second (shared across the per-beat fan-out).
    assert kw["system_instruction"][0] == "SKILL RULES"
    assert "beat 2 [solution" in kw["system_instruction"][1]  # timeline in block 2
    # sync_talk must NOT put the spoken line in the prompt request
    assert "this changed my mornings" not in kw["prompt"]
    assert "BASELINE FSTRING" in kw["prompt"]               # baseline handed as ref
    assert "LOCKED" in kw["prompt"]                          # anchor-lock instruction


@pytest.mark.asyncio
async def test_write_motion_prompt_native_talk_quotes_the_line(tmp_path):
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL")
    c = GeminiClient(_settings(), _client=object(),
                     skill_md_path=skill_path)   # _generate is mocked below,
    # so the provider is irrelevant; _client=object() avoids building genai.
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="p", restaged=True))
    beat = SimpleNamespace(
        index=0, ad_section="hook", shot_type="talking_head", framing="lav_mic",
        broll_type=None, broll_shot=None, dialogue_text="okay so listen",
        visual_note="she leans in", beat_kind="speech")
    char = _motion_char()
    await c.write_motion_prompt(
        beat=beat, character=char, window_len=4.0, mode="native_talk",
        uses_product=False, anchor_url="https://host/a0.png",
        timeline_overview="ov", fallback_example="BASE")
    kw = c._generate.call_args.kwargs
    assert '"okay so listen"' in kw["prompt"]   # line in quotes
    assert "Audio 1" in kw["prompt"]            # audio binding


@pytest.mark.asyncio
async def test_write_motion_prompt_broll_mode_directs_locked_camera(tmp_path):
    """B-roll mode must NOT quote a spoken line, and must carry the quick
    scale-only push-in camera guidance (2026-07-31: the old locked-off/static
    direction plus slow fallback push-ins read as stuck frames) + the
    broll_type tag in the user prompt."""
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL")
    c = GeminiClient(_settings(), _client=object(), skill_md_path=skill_path)
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="macro pour", restaged=False))
    beat = SimpleNamespace(
        index=4, ad_section="solution", shot_type="broll", framing=None,
        broll_type="texture_pour", broll_shot="serum dropper drip",
        dialogue_text="", visual_note="a drop forms on the dropper",
        beat_kind="speech")
    out = await c.write_motion_prompt(
        beat=beat, character=_motion_char(), window_len=3.0, mode="broll",
        uses_product=True, anchor_url="https://host/a4.png",
        timeline_overview="ov", fallback_example="BASE BROLL")
    assert out.prompt == "macro pour"
    kw = c._generate.call_args.kwargs
    assert kw["schema"] is MotionPrompt and kw["tier"] == "pro"
    assert "quick, smooth push-in" in kw["prompt"]  # decisive camera direction
    assert "never slow" in kw["prompt"]             # energy rule surfaced
    assert "broll texture_pour" in kw["prompt"]  # broll_type surfaced
    assert "product is in frame" in kw["prompt"] # uses_product=True surfaced
    assert '"' not in kw["prompt"].split("MODE: b-roll")[1]  # no quoted line in the b-roll task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,marker", [
    ("on_camera", "MODE: talking-head"),   # heygen_replicate talking head → sync_talk task
    ("background_vo", "MODE: b-roll"),      # background_vo cutaway → broll task
    ("silent", "MODE: b-roll"),             # silent cutaway → broll task
])
async def test_write_motion_prompt_accepts_voice_mode_taxonomy(tmp_path, mode, marker):
    """The seedance_native / heygen_replicate engines pass the voice-mode
    taxonomy (on_camera / background_vo / silent). write_motion_prompt must
    normalize those onto the task templates instead of KeyError-ing on the raw
    mode name (which silently disabled Opus prompt-writing for heygen_replicate).
    """
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL")
    c = GeminiClient(_settings(), _client=object(), skill_md_path=skill_path)
    c._generate = AsyncMock(return_value=MotionPrompt(prompt="p", restaged=False))
    beat = SimpleNamespace(
        index=0, ad_section="hook", shot_type="talking_head", framing=None,
        broll_type=None, broll_shot=None, dialogue_text="listen up",
        visual_note="she leans in", beat_kind="speech")
    out = await c.write_motion_prompt(
        beat=beat, character=_motion_char(), window_len=4.0, mode=mode,
        uses_product=False, anchor_url="https://host/a0.png",
        timeline_overview="ov", fallback_example="BASE")
    assert out.prompt == "p"  # did not fall back / raise
    assert marker in c._generate.call_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_write_motion_prompt_surfaces_restaged_flag(tmp_path):
    """The `restaged` flag from the returned MotionPrompt is surfaced verbatim
    by write_motion_prompt (the debug signal for 'Opus deviated from planner')."""
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL")
    c = GeminiClient(_settings(), _client=object(), skill_md_path=skill_path)
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="restaged prompt", restaged=True))
    beat = SimpleNamespace(
        index=1, ad_section="problem", shot_type="talking_head",
        framing="tripod", broll_type=None, broll_shot=None,
        dialogue_text="my skin was so dry", visual_note="she frowns",
        beat_kind="speech")
    out = await c.write_motion_prompt(
        beat=beat, character=_motion_char(), window_len=4.0, mode="sync_talk",
        uses_product=False, anchor_url="https://host/a1.png",
        timeline_overview="ov", fallback_example="BASE")
    assert out.restaged is True
    assert out.prompt == "restaged prompt"


@pytest.mark.asyncio
async def test_write_motion_prompt_folds_timeline_overview_into_system(tmp_path):
    """The full-timeline overview rides a cached system block (block 1), NOT
    the per-beat user prompt — and skill-MD is its own block (block 0) so it
    stays a shared cache prefix across methods."""
    from app.gemini import MotionPrompt
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("SKILL RULES BODY")
    c = GeminiClient(_settings(), _client=object(), skill_md_path=skill_path)
    c._generate = AsyncMock(
        return_value=MotionPrompt(prompt="p", restaged=False))
    beat = SimpleNamespace(
        index=0, ad_section="hook", shot_type="talking_head", framing="selfie",
        broll_type=None, broll_shot=None, dialogue_text="hey guys watch this",
        visual_note="she waves", beat_kind="speech")
    overview = "UNIQUE_TIMELINE_MARKER beat 0 -> beat 1 -> beat 2"
    await c.write_motion_prompt(
        beat=beat, character=_motion_char(), window_len=3.0, mode="sync_talk",
        uses_product=False, anchor_url="https://host/a.png",
        timeline_overview=overview, fallback_example="BASE")
    kw = c._generate.call_args.kwargs
    # Block 0 is skill-MD (shared prefix); block 1 carries the overview.
    assert kw["system_instruction"][0] == "SKILL RULES BODY"
    assert "UNIQUE_TIMELINE_MARKER" in kw["system_instruction"][1]
    assert "UNIQUE_TIMELINE_MARKER" not in kw["prompt"]   # not in the user turn


# ---------- Task 2: VoicePersona + render_voice_persona ----------

def test_render_voice_persona_pins_polished_indian_english():
    p = VoicePersona(gender="female", age_band="mid-20s",
                     timbre="warm bright", pace="calm conversational",
                     warmth="friendly")
    clause = render_voice_persona(p)
    assert "polished" in clause.lower()
    assert "indian" in clause.lower()
    # anti-stereotype guard must be present
    assert "not" in clause.lower() and "exaggerat" in clause.lower()


@pytest.mark.asyncio
async def test_describe_character_includes_voice_persona(monkeypatch):
    c = GeminiClient(_settings(), _client=_mock_genai_client("{}"))
    captured = {}

    async def fake_generate(*, tier, prompt, schema, **kw):
        captured["prompt"] = prompt
        return schema(  # build a minimal valid CharacterDescription
            age=25, gender="female", ethnicity="Indian", region="north_indian",
            skin_tone="fair_golden", hair_style="messy_high_bun",
            setting="bedroom_desk_warm", expression="calm_mid_sentence",
            hair="long", face="bright eyes", wardrobe="tank", energy="warm",
            voice_vibe="warm_relatable",
            voice_persona=VoicePersona(gender="female", age_band="mid-20s",
                timbre="warm bright", pace="calm", warmth="friendly"))

    monkeypatch.setattr(c, "_generate", fake_generate)
    result = await c.describe_character(brief="a skincare serum")
    assert result.voice_persona.gender == "female"
    assert "VOICE_PERSONA" in captured["prompt"] or "voice_persona" in captured["prompt"]


# ---------------------------------------------------------------------------
# voice_mode / dialogue_text consistency guard (Fix 1 — feat/seedance-native-timbre-voice)
# ---------------------------------------------------------------------------

def test_validate_rejects_silent_voice_mode_with_dialogue():
    """A beat with explicit voice_mode='silent' AND non-empty dialogue_text is
    contradictory — the validator must reject it.  This is the state that
    causes pipeline and assembly to disagree and crash a paid run."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta",
        "shot_type": "talking_head",
        "framing": "tripod",
        "dialogue_text": "I love this so much",
        "visual_note": "creator talks to camera",
        "voice_mode": "silent",
    })
    with pytest.raises(ValueError, match="voice_mode='silent'"):
        _validate_timeline(tl)


def test_validate_rejects_on_camera_voice_mode_with_empty_dialogue():
    """A beat with explicit voice_mode='on_camera' but empty dialogue_text
    promises a speaking clip that delivers silence — must be rejected."""
    from app.gemini import _validate_timeline
    # Build a valid hook beat; override beat 1 (the cta) with the bad state.
    tl = _timeline_with({
        "ad_section": "cta",
        "shot_type": "talking_head",
        "framing": "selfie",
        "dialogue_text": "",          # empty — contradicts on_camera
        "visual_note": "creator smiles at camera",
        "voice_mode": "on_camera",
    })
    # The dialogue_text partition check would also fire, so we need to fix
    # full_script to match the stripped beat.  Easiest: set full_script to
    # only beat 0's dialogue.
    object.__setattr__(
        tl, "full_script",
        tl.beats[0].dialogue_text,
    )
    with pytest.raises(ValueError, match="voice_mode="):
        _validate_timeline(tl)


def test_validate_accepts_consistent_silent_voice_mode():
    """voice_mode='silent' + empty dialogue_text is consistent — must pass."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta",
        "shot_type": "broll",
        "framing": None,
        "broll_type": "product_closeup",
        "dialogue_text": "",
        "visual_note": "pack on the counter, static shot",
        "voice_mode": "silent",
    })
    # full_script must match beat 0's dialogue only.
    object.__setattr__(tl, "full_script", tl.beats[0].dialogue_text)
    _validate_timeline(tl)   # must not raise


def test_validate_accepts_consistent_on_camera_voice_mode():
    """voice_mode='on_camera' + non-empty dialogue_text is consistent — must pass."""
    from app.gemini import _validate_timeline
    tl = _timeline_with({
        "ad_section": "cta",
        "shot_type": "talking_head",
        "framing": "tripod",
        "dialogue_text": "I love this so much",
        "visual_note": "creator talks to camera",
        "voice_mode": "on_camera",
    })
    _validate_timeline(tl)   # must not raise


def test_validate_skips_voice_mode_check_when_none():
    """voice_mode=None means the pipeline derives it; the validator must not
    fire for None even when the dialogue pattern would look odd."""
    from app.gemini import _validate_timeline
    # A broll with dialogue and voice_mode=None is fine — no explicit contract.
    tl = _timeline_with({
        "ad_section": "cta",
        "shot_type": "talking_head",
        "framing": "tripod",
        "dialogue_text": "I love this so much",
        "visual_note": "creator holds the pack",
        "voice_mode": None,
    })
    _validate_timeline(tl)   # must not raise


def test_empty_beats_is_rejected_by_the_schema_not_50_lines_later():
    """A sample that returned "beats": [] parsed cleanly (an empty list
    satisfies list[TimelineBeat]) and only failed later in _validate_timeline.
    Observed on the FIRST attempt of both 2026-08-06 planning calls, each after
    ~19.5k output tokens. min_length=1 puts minItems in the schema the model is
    handed and makes the SDK reject it at the boundary."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        VideoTimeline(full_script="hi", target_duration_s=20,
                      hook_type="result_first",
                      narrative_structure="testimonial", beats=[])
    assert VideoTimeline.model_json_schema()["properties"]["beats"]["minItems"] == 1


def test_claude_rejects_a_response_truncated_by_max_tokens():
    """On Opus max_tokens caps thinking + response together, so a long planning
    turn can die mid-JSON. Nothing checked stop_reason, so a truncated result
    could surface as a partially-filled object that looks valid."""
    from types import SimpleNamespace
    from app.claude import ClaudeBackend

    resp = SimpleNamespace(stop_reason="max_tokens",
                           usage=SimpleNamespace(output_tokens=32000))
    with pytest.raises(ValueError, match="token ceiling before finishing"):
        ClaudeBackend._raise_on_truncation(resp, 32000)
    # A normal completion passes untouched.
    ClaudeBackend._raise_on_truncation(
        SimpleNamespace(stop_reason="end_turn", usage=None), 32000)
