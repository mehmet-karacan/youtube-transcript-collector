import json

from youtube_transcript_collector.cli import main


def test_json_stdout_success_is_single_envelope(capsys, tmp_path):
    assert (
        main(
            ["--json", "--state-dir", str(tmp_path), "collect", "preview", "--video", "abcdefghijk"]
        )
        == 0
    )
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is True
    assert payload["data"]["network_used"] is False
    assert output.err == ""


def test_json_error_envelope(capsys):
    assert main(["--json", "targets", "resolve", "--video", "too-short"]) == 2
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_video_target"
    assert output.err == ""


def test_preview_exposes_validated_rate_policy(capsys):
    args = [
        "--json",
        "collect",
        "preview",
        "--video",
        "abcdefghijk",
        "--min-delay-seconds",
        "2.5",
        "--max-retries",
        "5",
        "--rate-limit-cooldown-seconds",
        "600",
    ]
    assert main(args) == 0
    policy = json.loads(capsys.readouterr().out)["data"]["request"]["rate_policy"]
    assert policy == {
        "min_delay_seconds": 2.5,
        "max_retries": 5,
        "rate_limit_cooldown_seconds": 600,
    }
    invalid = [
        "--json",
        "collect",
        "preview",
        "--video",
        "abcdefghijk",
        "--min-delay-seconds",
        "0",
    ]
    assert main(invalid) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_rate_policy"
