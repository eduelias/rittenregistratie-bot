import pytest

from rittenregistratie.cars import CarRegistry, normalize_phone


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


def test_duplicate_phone_rejected():
    with pytest.raises(ValueError):
        CarRegistry({
            "car_a": {"phones": ["31611112222"]},
            "car_b": {"phones": ["31611112222"]},
        })
