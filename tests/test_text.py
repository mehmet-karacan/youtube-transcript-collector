from pathlib import Path

from youtube_transcript_collector.text import safe_title, transcript_filename, vtt_to_text

FIXTURES = Path(__file__).parent / "fixtures"


def test_filename_policy_unicode_windows_and_budget():
    assert safe_title("  Türkçe   başlık  ") == "Türkçe başlık"
    assert safe_title("CON") == "_CON"
    assert safe_title('bad<>:"/\\|?* title. ') == "bad_________ title"
    value = safe_title("🚀" * 100, byte_budget=15)
    assert len(value.encode()) <= 15
    assert transcript_filename(None, "abcdefghijk", "A title").startswith(
        "unknown-date_abcdefghijk_"
    )
    assert safe_title("safe\u202eevil\u200b title") == "safeevil title"


def test_vtt_to_plain_text_deduplicates_caption_rollup():
    text = vtt_to_text((FIXTURES / "sample.vtt").read_text(encoding="utf-8"))
    assert text == "Hello world\nAgent-ready transcript.\n"
    assert "-->" not in text
