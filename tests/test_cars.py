import pytest

from rittenregistratie.cars import (
    ActiveCarStore, CarRegistry, add_car_to_yaml, add_phone_to_car_yaml, normalize_phone,
    remove_car_from_yaml, remove_phone_from_car_yaml,
)


def test_normalize_phone():
    assert normalize_phone("+31 6 1234 5678") == "31612345678"


def test_resolve_by_phone():
    reg = CarRegistry({
        "car_a": {"label": "A", "seed_odometer": 100, "phones": ["+31612345678"]},
        "car_b": {"label": "B", "seed_odometer": 200, "phones": ["31698765432"]},
    })
    assert reg.resolve("31612345678").car_id == "car_a"
    assert reg.resolve("+31698765432").car_id == "car_b"
    assert reg.resolve("31600000000") is None
    assert reg.is_registered("31612345678") and not reg.is_registered("31600000000")


def test_many_to_many_phones_and_cars():
    reg = CarRegistry({
        "shared": {"label": "Shared", "phones": ["31611112222", "31633334444"]},
        "van": {"label": "Delivery Van", "phones": ["31611112222"]},
    })
    # two people report for one car
    assert [c.car_id for c in reg.cars_for("31633334444")] == ["shared"]
    # one person drives two cars -> resolve() is ambiguous, cars_for() lists both
    assert reg.resolve("31611112222") is None
    assert [c.car_id for c in reg.cars_for("31611112222")] == ["shared", "van"]
    assert reg.find("van").car_id == "van"
    assert reg.find("delivery van").car_id == "van"  # by label, case-insensitive
    assert reg.find("nope") is None
    assert reg.get("van").matches("VAN") and reg.get("van").matches("Delivery Van")


def test_per_car_plugin_selection_parsed():
    reg = CarRegistry({
        "a": {"phones": ["1"], "event_plugins": ["x", "y"], "cap_plugin": "warn"},
        "b": {"phones": ["2"], "event_plugins": "x, z"},
        "c": {"phones": ["3"], "event_plugins": []},
        "d": {"phones": ["4"]},
    })
    assert reg.get("a").event_plugins == ["x", "y"] and reg.get("a").cap_plugin == "warn"
    assert reg.get("b").event_plugins == ["x", "z"] and reg.get("b").cap_plugin is None
    assert reg.get("c").event_plugins == []      # explicitly none
    assert reg.get("d").event_plugins is None    # use global default


def test_yaml_editing_many_to_many(tmp_path):
    path = tmp_path / "cars.yaml"
    add_car_to_yaml(path, "a", "Car A", "Home", 100, "31611112222")
    add_car_to_yaml(path, "b", "Car B", "Home", 200, "31611112222")  # same phone, 2nd car
    with pytest.raises(ValueError, match="already exists"):
        add_car_to_yaml(path, "a", "Dup", "Home", 1, "31600000000")
    assert add_phone_to_car_yaml(path, "a", "31633334444") is True
    assert add_phone_to_car_yaml(path, "a", "31633334444") is False
    with pytest.raises(ValueError, match="Unknown car"):
        add_phone_to_car_yaml(path, "zzz", "31633334444")
    reg = CarRegistry(__import__("yaml").safe_load(path.read_text()))
    assert sorted(c.car_id for c in reg.cars_for("31611112222")) == ["a", "b"]
    assert reg.get("a").phones == ["31611112222", "31633334444"]

    assert remove_phone_from_car_yaml(path, "a", "31633334444") is True
    assert remove_phone_from_car_yaml(path, "a", "31633334444") is False

    # remove by phone: detached from both; both cars left empty -> deleted
    res = remove_car_from_yaml(path, "31611112222")
    assert sorted(res.detached_from) == ["a", "b"] and sorted(res.removed_cars) == ["a", "b"]
    assert not remove_car_from_yaml(path, "31611112222")

    add_car_to_yaml(path, "s", "Shared", "Home", 1, "31611112222")
    add_phone_to_car_yaml(path, "s", "31633334444")
    res = remove_car_from_yaml(path, "31611112222")   # car keeps the other phone
    assert res.detached_from == ["s"] and res.removed_cars == []
    res = remove_car_from_yaml(path, "s")              # by car id
    assert res.removed_cars == ["s"]


def test_active_car_store(tmp_path):
    store = ActiveCarStore(tmp_path / "active.json")
    assert store.get("31611112222") is None
    store.set("+31 611112222", "van")
    assert store.get("31611112222") == "van"
    store.clear("31611112222")
    assert store.get("31611112222") is None
