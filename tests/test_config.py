

def test_saving_a_location_again_keeps_what_it_already_knew(tmp_path):
    """A place is taught in pieces: coordinates by the car, the address by chat."""
    from rittenregistratie.config import load_yaml, save_location
    path = tmp_path / "locations-learned.yaml"
    save_location(path, "gym", "De Diagonaal 195, Almere",
                  lat=52.36922, lon=5.22008, radius_m=150, private=True)
    # the driver later answers an address prompt for the same place
    save_location(path, "gym", "De Diagonaal 195, 1315 XM Almere")
    entry = load_yaml(path)["gym"]
    assert entry["address"] == "De Diagonaal 195, 1315 XM Almere"   # refreshed
    assert entry["lat"] == 52.36922 and entry["lon"] == 5.22008     # kept
    assert entry["radius_m"] == 150 and entry["private"] is True    # kept


def test_saving_a_location_can_still_change_a_field(tmp_path):
    from rittenregistratie.config import load_yaml, save_location
    path = tmp_path / "locations-learned.yaml"
    save_location(path, "gym", "A St", radius_m=150, private=True)
    save_location(path, "gym", "A St", radius_m=400)
    entry = load_yaml(path)["gym"]
    assert entry["radius_m"] == 400 and entry["private"] is True
