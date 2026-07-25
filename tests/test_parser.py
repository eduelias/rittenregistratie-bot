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
