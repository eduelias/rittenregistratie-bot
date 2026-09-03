"""Event bus: identity default, ordered chaining, plugin loading."""
from rittenregistratie import events
from rittenregistratie.events import EventBus, load_event_plugins


def test_emit_without_handlers_is_identity():
    bus = EventBus()
    payload = {"trips": [1, 2, 3]}
    assert bus.emit("export.pre_generate", payload) is payload


def test_handlers_chain_in_order_and_none_keeps_payload():
    bus = EventBus()
    bus.on("e", lambda p: p + ["a"])
    bus.on("e", lambda p: None)  # observer: leaves payload untouched
    bus.on("e", lambda p: p + ["b"])
    assert bus.emit("e", []) == ["a", "b"]
    assert len(bus.handlers("e")) == 3 and bus.handlers("other") == []


class _EP:
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def load(self):
        return self._fn


def test_load_event_plugins_all_and_selected(monkeypatch):
    calls = []

    def reg_a(bus):
        calls.append("a"); bus.on("x", lambda p: p + 1)

    def reg_b(bus):
        calls.append("b"); bus.on("x", lambda p: p * 10)

    def broken(bus):
        raise RuntimeError("boom")

    fake = [_EP("a", reg_a), _EP("b", reg_b), _EP("bad", broken)]
    monkeypatch.setattr(events, "entry_points", lambda group: fake)

    bus = EventBus()
    assert load_event_plugins(bus) == ["a", "b"]  # broken one skipped, not raised
    assert bus.emit("x", 1) == 20

    bus = EventBus()
    assert load_event_plugins(bus, ["b", "missing"]) == ["b"]
    assert bus.emit("x", 1) == 10

    bus = EventBus()
    assert load_event_plugins(bus, []) == []
    assert bus.emit("x", 1) == 1
