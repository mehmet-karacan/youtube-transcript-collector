import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from youtube_transcript_collector.cli import _failure, _success
from youtube_transcript_collector.errors import CollectorError
from youtube_transcript_collector.targets import build_request

SCHEMAS = Path(__file__).parents[1] / "schemas"


def _schema(name):
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def test_schemas_are_valid_and_accept_runtime_payloads():
    request_schema = _schema("collection-request.schema.json")
    envelope_schema = _schema("cli-envelope.schema.json")
    Draft202012Validator.check_schema(request_schema)
    Draft202012Validator.check_schema(envelope_schema)
    Draft202012Validator(request_schema, format_checker=FormatChecker()).validate(
        build_request(videos=["abcdefghijk"]).as_dict()
    )
    envelope = Draft202012Validator(envelope_schema)
    envelope.validate(_success({"value": 1}))
    envelope.validate(_failure(CollectorError("bad_request", "bad request")))


def test_envelope_requires_success_xor_failure():
    validator = Draft202012Validator(_schema("cli-envelope.schema.json"))
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "ok": True,
                "data": {},
                "error": {"code": "x", "message": "x", "details": {}},
                "schema_version": 1,
            }
        )
    with pytest.raises(ValidationError):
        validator.validate({"ok": False, "schema_version": 1})


def test_request_schema_rejects_runtime_invariant_violations():
    validator = Draft202012Validator(
        _schema("collection-request.schema.json"), format_checker=FormatChecker()
    )
    valid = build_request(videos=["abcdefghijk"]).as_dict()
    invalid_payloads = []

    two_single = copy.deepcopy(valid)
    two_single["targets"].append("https://www.youtube.com/watch?v=lmnopqrstuv")
    invalid_payloads.append(two_single)

    wrong_target = copy.deepcopy(valid)
    wrong_target["targets"] = ["https://www.youtube.com/@FixtureChannel123"]
    invalid_payloads.append(wrong_target)

    channel = build_request(channel="@FixtureChannel123", latest=3).as_dict()
    too_many = copy.deepcopy(channel)
    too_many["selector"]["latest"] = 10001
    invalid_payloads.append(too_many)

    blank_title = copy.deepcopy(channel)
    blank_title["selector"]["latest"] = None
    blank_title["selector"]["title_query"] = "   "
    invalid_payloads.append(blank_title)

    channel_all = build_request(channel="@FixtureChannel123").as_dict()
    channel_all["selector"]["latest"] = 1
    invalid_payloads.append(channel_all)

    wildcard_language = copy.deepcopy(valid)
    wildcard_language["languages"] = ["all"]
    invalid_payloads.append(wildcard_language)

    oversized_title = copy.deepcopy(channel)
    oversized_title["selector"]["latest"] = None
    oversized_title["selector"]["title_query"] = "x" * 201
    invalid_payloads.append(oversized_title)

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            validator.validate(payload)
