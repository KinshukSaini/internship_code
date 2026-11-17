"""Cost accounting: pricing maths, ledger behaviour, and rendering."""
from types import SimpleNamespace

import pytest

from app import costs


# ── Claude ───────────────────────────────────────────────────────────────

def _usage(**kw):
    base = dict(input_tokens=0, output_tokens=0,
                cache_read_input_tokens=0, cache_creation_input_tokens=0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_claude_prices_plain_input_and_output():
    usd, _ = costs.price_claude(
        "claude-opus-4-8", _usage(input_tokens=1_000_000,
                                  output_tokens=1_000_000))
    assert usd == pytest.approx(30.0)          # $5 in + $25 out


def test_claude_cache_read_is_a_tenth_of_input():
    read, _ = costs.price_claude(
        "claude-opus-4-8", _usage(cache_read_input_tokens=1_000_000))
    fresh, _ = costs.price_claude(
        "claude-opus-4-8", _usage(input_tokens=1_000_000))
    assert read == pytest.approx(fresh * 0.10)


def test_claude_cache_write_carries_the_premium():
    write, _ = costs.price_claude(
        "claude-opus-4-8", _usage(cache_creation_input_tokens=1_000_000))
    assert write == pytest.approx(6.25)        # $5 x 1.25


def test_claude_counts_cached_tokens_on_top_of_input_not_inside_it():
    """input_tokens is the UNCACHED remainder — the three must sum, or a
    heavily-cached run under-reports its prompt cost."""
    usd, detail = costs.price_claude("claude-haiku-4-5", _usage(
        input_tokens=1000, cache_read_input_tokens=9000,
        cache_creation_input_tokens=500, output_tokens=200))
    expected = (1000 * 1.0 + 9000 * 0.1 + 500 * 1.25 + 200 * 5.0) / 1e6
    assert usd == pytest.approx(expected)
    assert detail["cache_read_tokens"] == 9000


def test_unknown_claude_model_is_unpriced_not_free():
    assert costs.price_claude("claude-from-the-future", _usage()) is None


# ── Gemini ───────────────────────────────────────────────────────────────

def test_gemini_text_bills_thinking_as_output():
    usd, detail = costs.price_gemini_text("gemini-3.5-flash", SimpleNamespace(
        prompt_token_count=1_000_000, candidates_token_count=500_000,
        thoughts_token_count=500_000))
    assert detail["output_tokens"] == 1_000_000
    assert usd == pytest.approx(1.50 + 9.00)


def test_gemini_pro_switches_to_the_long_context_tier():
    short, _ = costs.price_gemini_text("gemini-3.1-pro-preview",
                                       SimpleNamespace(prompt_token_count=1000))
    long, _ = costs.price_gemini_text("gemini-3.1-pro-preview",
                                      SimpleNamespace(prompt_token_count=300_000))
    assert long / 300 == pytest.approx(short / 1 * 2, rel=1e-6)


def test_nano_banana_pro_2k_image_matches_published_price():
    usd, _ = costs.price_gemini_image("gemini-3-pro-image-preview", "2K")
    assert usd == pytest.approx(0.1344, abs=1e-4)


def test_nano_banana_2_prices_by_rendered_size():
    one_k, _ = costs.price_gemini_image("gemini-3.1-flash-image-preview", "1K")
    four_k, _ = costs.price_gemini_image("gemini-3.1-flash-image-preview", "4K")
    assert one_k == pytest.approx(0.0672, abs=1e-4)
    assert four_k == pytest.approx(0.1512, abs=1e-4)


def test_image_input_tokens_are_billed_when_references_are_sent():
    bare, _ = costs.price_gemini_image("gemini-3-pro-image-preview", "2K")
    with_refs, detail = costs.price_gemini_image(
        "gemini-3-pro-image-preview", "2K",
        SimpleNamespace(prompt_token_count=100_000))
    assert with_refs > bare
    assert with_refs - bare == pytest.approx(100_000 * 2.00 / 1e6)
    assert detail["prompt_tokens"] == 100_000


def test_omni_second_is_about_ten_cents():
    assert costs.price_omni_video(1) == pytest.approx(0.10, abs=0.005)


# ── EvoLink ──────────────────────────────────────────────────────────────

def test_evolink_prefers_the_dollar_figure_the_provider_reports():
    usd, authoritative = costs.price_evolink(
        {"usage": {"cost": {"credits": 54, "usd": 0.795}, "credits_used": 54}})
    assert (usd, authoritative) == (0.795, True)


def test_evolink_falls_back_to_credits_then_to_seconds():
    by_credits, auth = costs.price_evolink({"usage": {"credits_used": 54}})
    assert by_credits == pytest.approx(0.795, abs=0.001)
    assert auth is False

    by_seconds, auth = costs.price_evolink({}, provider="seedance", seconds=4)
    assert by_seconds == pytest.approx(0.795, abs=0.001)
    assert auth is False


def test_kling_is_priced_cheaper_than_seedance_per_second():
    kling, _ = costs.price_evolink({}, provider="kling", seconds=10)
    seedance, _ = costs.price_evolink({}, provider="seedance", seconds=10)
    assert kling < seedance


# ── Ledger ───────────────────────────────────────────────────────────────

def test_clip_waste_is_the_share_of_the_clip_the_cut_throws_away():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_clip(
            task_id="t1", provider="seedance", model="seedance-2.0",
            ordered_seconds=4.0, used_seconds=3.0,
            raw={"usage": {"cost": {"usd": 0.80}}})
    entry = ledger.entries[0]
    assert entry.usd == 0.80
    assert entry.wasted_usd == pytest.approx(0.20)   # 1 of 4 seconds unused
    assert entry.authoritative is True


def test_re_polling_a_paid_task_does_not_double_bill_it():
    """Resume polls an already-completed task; its cost was paid once."""
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        for _ in range(3):
            costs.record_clip(
                task_id="same-task", provider="seedance", model="m",
                ordered_seconds=4.0, used_seconds=4.0,
                raw={"usage": {"cost": {"usd": 0.80}}})
    assert len(ledger.entries) == 1
    assert ledger.total_usd == pytest.approx(0.80)


def test_a_retried_clip_is_billed_twice_because_it_was_paid_twice():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        for task in ("attempt-1", "attempt-2"):
            costs.record_clip(
                task_id=task, provider="seedance", model="m",
                ordered_seconds=4.0, used_seconds=4.0,
                raw={"usage": {"cost": {"usd": 0.80}}})
    assert ledger.total_usd == pytest.approx(1.60)


def test_recording_outside_a_run_is_a_no_op_not_a_crash():
    """The image playground tab and one-off scripts call the same clients."""
    assert costs.active() is None
    costs.record_tts(provider="elevenlabs", model="eleven_v3", characters=100)
    costs.note_unpriced("nothing")


def test_ledger_does_not_leak_between_runs():
    first = costs.start_run()
    costs.record_tts(provider="elevenlabs", model="eleven_v3", characters=1000)
    second = costs.start_run()
    assert second is not first
    assert second.total_usd == 0.0
    assert first.total_usd == pytest.approx(0.10)


def test_elevenlabs_bills_ten_cents_per_thousand_characters():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_tts(provider="elevenlabs", model="eleven_v3",
                         characters=1000)
    assert ledger.total_usd == pytest.approx(0.10)


def test_an_unrateable_provider_is_flagged_rather_than_billed_as_free():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_tts(provider="cartesia", model="sonic-3.5",
                         characters=1000)
    assert ledger.entries == []
    assert "tts:cartesia:sonic-3.5" in ledger.snapshot()["unpriced"]


def test_lipsync_rates_differ_by_engine():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_lipsync(provider="syncso", model="sync-3", seconds=10)
        costs.record_lipsync(provider="replicate",
                             model="heygen/lipsync-precision", seconds=10)
    syncso, replicate = ledger.entries
    assert syncso.usd == pytest.approx(0.50)
    assert replicate.usd == pytest.approx(0.667)


def test_by_component_blanks_the_unit_when_quantities_are_incomparable():
    """Summing tokens and images into one number would be a lie."""
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_gemini_image("gemini-3.1-flash-image-preview", "2K")
        costs.record_clip(task_id="t", provider="seedance", model="m",
                          ordered_seconds=4, used_seconds=4,
                          raw={"usage": {"cost": {"usd": 0.8}}})
        ledger.add(costs.CostEntry(
            component="images", provider="x", model="y",
            unit="token", qty=5, usd=0.0))
    images = ledger.by_component()["images"]
    assert images["unit"] == ""
    assert images["qty"] == 0.0
    assert ledger.by_component()["video_clips"]["unit"] == "s"


def test_snapshot_totals_match_the_entries():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_clip(task_id="a", provider="seedance", model="m",
                          ordered_seconds=4, used_seconds=2,
                          raw={"usage": {"cost": {"usd": 1.00}}})
        costs.record_tts(provider="elevenlabs", model="eleven_v3",
                         characters=500)
    snap = ledger.snapshot()
    assert snap["total_usd"] == pytest.approx(1.05)
    assert snap["wasted_usd"] == pytest.approx(0.50)
    assert snap["rate_table_version"] == costs.RATE_TABLE_VERSION
    assert len(snap["entries"]) == 2


# ── Rendering ────────────────────────────────────────────────────────────

def test_markdown_shows_the_total_the_waste_and_the_unpriced_gap():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_clip(task_id="a", provider="seedance", model="m",
                          ordered_seconds=4, used_seconds=3,
                          raw={"usage": {"cost": {"usd": 1.00}}})
        costs.note_unpriced("tts:cartesia:sonic-3.5")
    md = costs.format_markdown(ledger.snapshot())
    assert "**$1.00**" in md
    assert "Waste $0.25" in md
    assert "4.0s ordered → 3.0s used" in md
    assert "cartesia" in md


def test_markdown_omits_the_waste_line_when_nothing_was_wasted():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_tts(provider="elevenlabs", model="eleven_v3",
                         characters=1000)
    assert "Waste" not in costs.format_markdown(ledger.snapshot())


def test_markdown_handles_a_run_with_nothing_priced():
    assert "No priced calls" in costs.format_markdown(
        costs.CostLedger().snapshot())


def test_progress_line_is_a_single_readable_total():
    ledger = costs.CostLedger()
    with costs.activate(ledger):
        costs.record_clip(task_id="a", provider="seedance", model="m",
                          ordered_seconds=4, used_seconds=4,
                          raw={"usage": {"cost": {"usd": 2.50}}})
    assert costs.format_line(ledger.snapshot()) == "total cost $2.50"
