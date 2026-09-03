"""RouteBook: case-insensitive names, alias resolution, merged sources."""
from rittenregistratie.routes import RouteBook, normalize_name


def test_normalize_name():
    assert normalize_name("  Home ") == "home"
    assert normalize_name("Shr   and  Back") == "shr and back"
    assert normalize_name("") == ""


def test_lookup_is_case_and_space_insensitive():
    rb = RouteBook({"Home": {"address": "A St 1, Utrecht"}}, {})
    assert rb.is_known("home")
    assert rb.is_known(" HOME ")
    assert rb.address_for("home") == "A St 1, Utrecht"
    assert rb.address_for("Home") == rb.address_for("home")


def test_alias_to_alias_resolves_to_final_address():
    rb = RouteBook(
        {
            "rotterdam": {"address": "Oostmaaslaan 53, Rotterdam"},
            "rtm": {"address": "rotterdam"},
            "rtm7": {"address": "RTM"},
        },
        {},
    )
    assert rb.address_for("rtm7") == "Oostmaaslaan 53, Rotterdam"
    assert rb.address_for("rtm") == "Oostmaaslaan 53, Rotterdam"


def test_alias_cycle_terminates():
    rb = RouteBook({"a": {"address": "b"}, "b": {"address": "a"}}, {})
    assert rb.address_for("a") in ("a", "b")


def test_unknown_name_returned_unchanged():
    rb = RouteBook({}, {})
    assert rb.address_for("Somewhere 12, Almere") == "Somewhere 12, Almere"
    assert not rb.is_known("Somewhere 12, Almere")


def test_learned_locations_merge_and_override():
    seed = {"Home": {"address": "Old"}, "Office": {"address": "B St"}}
    learned = {"home": {"address": "New"}, "gym": {"address": "C St"}}
    rb = RouteBook(seed, {}, learned)
    assert rb.address_for("Home") == "New"  # learned wins for same place
    assert rb.address_for("Office") == "B St"
    assert rb.address_for("Gym") == "C St"


def test_route_keys_normalized():
    rb = RouteBook({}, {"Home->Office": {"expected_km": 40, "variants": [38, 40]}})
    info = rb.lookup("home", "OFFICE")
    assert info is not None and info.expected_km == 40
    assert info.nearest_variant(39) in (38, 40)
