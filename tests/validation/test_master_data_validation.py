from scripts.validation.validate_master_data import headers_match_schema


def test_master_headers_accept_only_a_missing_optional_trailing_suffix() -> None:
    expected = ["ID", "Required", "OptionalA", "OptionalB"]
    optional = {"OptionalA", "OptionalB"}

    assert headers_match_schema(expected, expected, optional)
    assert headers_match_schema(expected[:2], expected, optional)
    assert not headers_match_schema(expected[:3], expected, set())
    assert not headers_match_schema(["ID", "OptionalA"], expected, optional)
    assert not headers_match_schema(expected + ["Unexpected"], expected, optional)
