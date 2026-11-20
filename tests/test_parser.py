from rittenregistratie.parser import ParseError, parse_message
import pytest


def test_basic():
    m = parse_message("145230 Office")
    assert m.end_odo == 145230
    assert m.destination == "Office"
    assert not m.is_private


def test_private_keyword():
    m = parse_message("145230 Office private")
    assert m.destination == "Office"
    assert m.is_private


def test_private_tag_and_note():
    m = parse_message("145230 Kantoor Amsterdam #private -- lunch client")
    assert m.is_private
    assert m.destination == "Kantoor Amsterdam"
    assert m.note == "lunch client"


def test_bare_private():
    m = parse_message("200000 private")
    assert m.is_private
    assert m.destination == "private"


def test_missing_odo():
    with pytest.raises(ParseError):
        parse_message("Office")


def test_empty():
    with pytest.raises(ParseError):
        parse_message("   ")


def test_private_in_parens_cleaned():
    m = parse_message("18832 home (private)")
    assert m.destination == "home"
    assert m.is_private
    assert m.note == ""


def test_empty_parens_removed():
    m = parse_message("18832 Office ()")
    assert m.destination == "Office"
    assert not m.is_private


def test_standalone_marker_is_not_part_of_the_destination():
    """A '*' the user puts on a message must not stop a place being recognised."""
    parsed = parse_message("20672 den haag *")
    assert parsed.destination == "den haag"
    assert parsed.raw == "20672 den haag *"  # kept verbatim for the ledger


def test_marker_and_private_together():
    parsed = parse_message("20672 den haag private *")
    assert parsed.destination == "den haag"
    assert parsed.is_private


def test_an_asterisk_inside_a_word_is_left_alone():
    assert parse_message("20672 a*b").destination == "a*b"


def test_dutch_prive_marks_a_private_trip():
    for word in ("privé", "Prive", "#privé"):
        parsed = parse_message(f"45260 Thuis {word}")
        assert parsed.is_private and parsed.destination == "Thuis"


def test_prive_inside_a_word_is_a_place():
    assert not parse_message("45260 Privékliniek").is_private


def test_a_note_that_is_only_the_word_marks_the_trip_private():
    m = parse_message("23250 Supermarkt -- privé")
    assert m.is_private and m.destination == "Supermarkt"
    assert not parse_message("23250 Klant -- niet privé gereden").is_private


def test_private_glued_with_a_hyphen_is_part_of_the_name():
    m = parse_message("23250 non-private parking")
    assert not m.is_private and m.destination == "non-private parking"


def test_a_digit_that_is_not_a_number_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_message("² office")
