"""Bot texts in Dutch and English, chosen per phone number.

Every number has a language: what it chose with ``taal`` / ``language``
(stored in ``data/languages.json``), else ``RIT_DEFAULT_LANGUAGE`` (Dutch).
The webhook sets the sender's language for the message it is handling
(:func:`set_current`); code that writes to someone else (an admin, every driver
of a shared car, a customer after a Mollie webhook) passes ``lang=``.

    t("trip.lower", label="Golf", odo=100, last=120)

Ledger data (notes, addresses, the raw message) is never translated: it is a
record, not a message. Owner statistics (analytics.py) are always English.
"""
from __future__ import annotations

import json
import threading
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Dict, Optional

LANGS = ("nl", "en")
_fallback = "nl"
_current: ContextVar[Optional[str]] = ContextVar("rit_lang", default=None)


def set_default(lang: str) -> None:
    """The language of numbers that did not choose one (RIT_DEFAULT_LANGUAGE)."""
    global _fallback
    _fallback = lang if lang in LANGS else "nl"


def default() -> str:
    return _fallback


def set_current(lang: Optional[str]) -> None:
    """The language of the message being handled now (per request task)."""
    _current.set(lang if lang in LANGS else None)


def current() -> str:
    return _current.get() or _fallback


def t(key: str, lang: Optional[str] = None, **kw) -> str:
    entry = CATALOG[key]
    text = entry[1] if (lang or current()) == "en" else entry[0]
    return text.format(**kw) if kw else text


class Key(str):
    """A catalog key used as an argument value: rendered in the target language."""


def render_parts(parts, lang: Optional[str] = None) -> str:
    """Lines from ``str`` or ``(key, kwargs)`` parts; Key values are translated."""
    out = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
            continue
        key, kw = part
        kw = {k: (t(v, lang) if isinstance(v, Key) else v) for k, v in kw.items()}
        out.append(t(key, lang, **kw))
    return "\n".join(out)


_MONTHS = {
    "nl": ("januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
           "september", "oktober", "november", "december"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"),
}


def date_text(value, lang: Optional[str] = None) -> str:
    """``14 november 2026`` / ``14 November 2026``."""
    d = date.fromisoformat(value) if isinstance(value, str) else value
    lang = lang or current()
    return f"{d.day} {_MONTHS[lang][d.month - 1]} {d.year}"


def money(value, lang: Optional[str] = None) -> str:
    """``4,95`` / ``4.95`` (no currency sign)."""
    text = f"{float(value):.2f}"
    return text.replace(".", ",") if (lang or current()) == "nl" else text


# Words that pick a language, and the bare commands that show the choice.
CHOICES = {
    "nederlands": "nl", "dutch": "nl", "nl": "nl",
    "english": "en", "engels": "en", "en": "en",
}
LANGUAGE_WORDS = ("taal", "language", "lang")


def parse_language_command(text: str):
    """``(True, lang|None)`` for a language command, ``(False, None)`` otherwise.

    ``english``, ``nederlands``, ``taal en``, ``language nl``: a choice.
    ``taal`` / ``language`` alone: show the current one (lang None).
    """
    words = (text or "").strip().lower().lstrip("/").split()
    if not words:
        return False, None
    if len(words) == 1 and words[0] in CHOICES and words[0] not in ("nl", "en"):
        return True, CHOICES[words[0]]
    if words[0] in LANGUAGE_WORDS:
        if len(words) == 1:
            return True, None
        if len(words) == 2 and words[1] in CHOICES:
            return True, CHOICES[words[1]]
        return True, None
    return False, None


class LanguageStore:
    """``data/languages.json``: {phone: "nl" | "en"}."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except ValueError:
            return {}

    def get(self, phone: str) -> Optional[str]:
        from .cars import normalize_phone
        return self._load().get(normalize_phone(phone))

    def of(self, phone: str) -> str:
        """The language to write to this number in."""
        return self.get(phone) or _fallback

    def set(self, phone: str, lang: str) -> None:
        from .cars import normalize_phone
        if lang not in LANGS:
            raise ValueError(lang)
        with self._lock:
            data = self._load()
            data[normalize_phone(phone)] = lang
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# key -> (Dutch, English)
CATALOG: Dict[str, tuple] = {
    # --- language ---------------------------------------------------------
    "lang.set": ("Prima, ik antwoord voortaan in het Nederlands. Send 'english' for English.",
                 "OK, I'll reply in English from now on. Stuur 'nederlands' voor Nederlands."),
    "lang.show": ("Ik antwoord in het Nederlands. Send 'english' for English.",
                  "I reply in English. Stuur 'nederlands' voor Nederlands."),

    # --- help / cars --------------------------------------------------------
    "help.user": (
        "Welkom bij RittenRegistratie ({label}).\n"
        "Stuur na elke rit je kilometerstand en bestemming:\n"
        "  45230 Kantoor\n"
        "  45260 Thuis privé\n"
        "Onbekende bestemming? Ik vraag één keer om het adres.\n"
        "'excel' stuurt je rittenoverzicht van dit jaar, 'excel 2025' een ander jaar.\n"
        "'abonnement' toont je abonnement met een genummerd menu (antwoord met 1, 2, ...).\n"
        "Direct kan ook: 'pauzeren', 'hervatten', 'opzeggen', 'opnieuw', "
        "'maandelijks' of 'jaarlijks' (overstappen).\n"
        "'factuur' stuurt je laatste factuur, 'facturen' toont ze allemaal, "
        "'factuurgegevens' je bedrijfsnaam en factuuradres.\n"
        "Send 'english' for English.",
        "Welcome to RittenRegistratie ({label}).\n"
        "After every trip, send your odometer reading and destination:\n"
        "  45230 Office\n"
        "  45260 Home private\n"
        "Unknown destination? I ask for the address once.\n"
        "'excel' sends this year's trip log, 'excel 2025' another year.\n"
        "'subscription' shows your subscription with a numbered menu (reply 1, 2, ...).\n"
        "Or directly: 'pause', 'resume', 'unsubscribe', 'restart', "
        "'monthly' or 'yearly' (switch plan).\n"
        "'invoice' sends your latest invoice, 'invoices' lists them all, "
        "'billing details' shows your company name and billing address.\n"
        "Stuur 'nederlands' voor Nederlands."),
    "car.unknown_list": ("Onbekende auto '{hint}'. Jouw auto's: {cars}.",
                         "Unknown car '{hint}'. Your cars: {cars}."),
    "car.unknown": ("Onbekende auto '{hint}'.", "Unknown car '{hint}'."),
    "car.not_registered": ("Nummer {phone} hoort bij geen enkele auto.",
                           "Number {phone} is not registered to any car."),
    "car.several": (
        "Je rijdt voor meerdere auto's: {cars}. Stuur 'car <id>' om er een te kiezen, of begin "
        "je bericht met de id van de auto, bijvoorbeeld '{example} 145230 Kantoor'.",
        "You report for several cars: {cars}. Send 'car <id>' to choose one, or start the "
        "message with the car id, e.g. '{example} 145230 Office'."),
    "car.none": ("geen", "none"),
    "car.only_own": ("Je kunt alleen een van je eigen auto's kiezen.",
                     "You can only activate one of your own cars."),
    "car.active": ("Actieve auto: {label} [{id}].", "Active car: {label} [{id}]."),
    "car.list": ("Jouw auto's:", "Your cars:"),
    "car.active_mark": (" (actief)", " (active)"),
    "car.switch_hint": ("Wissel met 'car <id>' of begin je bericht met de id van de auto.",
                        "Switch with 'car <id>' or prefix a message with the car id."),
    "name.usage": ("Gebruik: name <plaats>  (geeft het laatste onbekende rit-einde een naam).",
                   "Usage: name <place>  (names the last unnamed trip end)."),
    "name.nothing": ("Niets te benoemen: de laatste automatische rit eindigde op een bekende plek.",
                     "Nothing to name: the last automatic trip ended at a known place."),
    "name.saved": ("'{place}' opgeslagen: {address}. Volgende ritten die daar eindigen gebruiken het.",
                   "Saved '{place}': {address}. Future trips ending there will use it."),
    "vehicle.unknown_place": ("Deze plek ken ik niet. Antwoord 'name <plaats>' om hem te leren.",
                              "I don't know this place. Reply 'name <place>' to teach me it."),

    # --- trips --------------------------------------------------------------
    "trip.summary": ("[{label}] {origin} -> {destination} ({km} km, {purpose}).",
                     "[{label}] {origin} -> {destination} ({km} km, {purpose})."),
    "trip.odometer": ("Kilometerstand {odo}.", "Odometer {odo}."),
    "trip.saved_address": ("Adres voor '{destination}' opgeslagen: {address}",
                           "Saved address for '{destination}': {address}"),
    "trip.route": ("Route vastgelegd (afwijking): {route}", "Route recorded (deviation): {route}"),
    "purpose.business": ("zakelijk", "business"),
    "purpose.private": ("privé", "private"),
    "trip.already": ("[{label}] Al vastgelegd: kilometerstand {odo}. Niets toegevoegd: de "
                     "kilometerstand is sindsdien niet veranderd.",
                     "[{label}] Already logged: odometer {odo}. Nothing added — the odometer "
                     "has not moved since."),
    "trip.already_at": ("[{label}] Al vastgelegd: kilometerstand {odo} bij {place}. Niets "
                        "toegevoegd: de kilometerstand is sindsdien niet veranderd.",
                        "[{label}] Already logged: odometer {odo} at {place}. Nothing added — "
                        "the odometer has not moved since."),
    "trip.lower": ("[{label}] kilometerstand {odo} is lager dan de laatst vastgelegde {last}.",
                   "[{label}] odometer {odo} is lower than last recorded {last}."),
    "trip.ask_address": (
        "Ik heb nog geen adres voor '{destination}'.\n"
        "Antwoord met het volledige adres, de naam van een bekende plek, of deel de locatie "
        "(WhatsApp bijlage → Locatie). Stuur 'annuleer' om deze rit te laten vallen.",
        "I don't have an address for '{destination}'.\n"
        "Please reply with the full address, the name of a known location, or share the "
        "location (WhatsApp attach → Location). Send 'cancel' to drop this trip."),
    "loc.not_waiting": ("Bedankt, maar ik wachtte niet op een locatie. Stuur eerst "
                        "<kilometerstand> <bestemming>.",
                        "Thanks, but I wasn't waiting for a location. Send "
                        "<odometer> <destination> first."),
    "addr.reply": ("Antwoord met het adres, of deel de locatie.",
                   "Please reply with the address, or share the location."),
    "trip.cancelled": ("Geannuleerd: de rit naar '{destination}' (kilometerstand {odo}) is niet "
                       "vastgelegd.",
                       "Cancelled: the trip to '{destination}' (odometer {odo}) was not logged."),
    "trip.dropped": ("De wachtende rit naar '{destination}' (kilometerstand {odo}) is vervallen: er "
                     "kwam geen adres, dus hij is niet vastgelegd.\n",
                     "Dropped the pending trip to '{destination}' (odometer {odo}): no address "
                     "was given, so it was not logged.\n"),
    "addr.invalid": ("Dat lijkt geen adres. Antwoord met een straat en plaats (bijv. "
                     "'Waldorpstraat 3, Den Haag') of de naam van een bekende plek, deel de "
                     "locatie, of stuur 'annuleer'.",
                     "That doesn't look like an address. Reply with a street address (e.g. "
                     "'Waldorpstraat 3, Den Haag') or the name of a known location, share the "
                     "location, or send 'cancel'."),
    "cap.over": ("LET OP: privé {km} km is {over} km boven de grens van {cap} km voor {year}.",
                 "WARNING: private {km} km exceeds the {cap} km cap by {over} km for {year}."),
    "cap.status": ("Privé {km}/{cap} km (nog {left} km).",
                   "Private {km}/{cap} km ({left} km left)."),
    "parse.empty": ("Leeg bericht. Stuur: <kilometerstand> <bestemming> [privé]",
                    "Empty message. Send: <odometer> <destination> [private]"),
    "parse.no_odo": ("De kilometerstand ontbreekt.", "Missing odometer reading."),
    "parse.no_dest": ("De bestemming ontbreekt. Stuur: <kilometerstand> <bestemming>.",
                      "Missing destination. Send: <odometer> <destination>."),
    "trip.usage": ("Stuur: <kilometerstand> <bestemming> [privé], of deel een locatie als ik "
                   "daarom vraag.",
                   "Please send: <odometer> <destination> [private], or share a location "
                   "when asked."),
    "trip.could_not": ("Rit niet vastgelegd: {error}", "Could not log trip: {error}"),
    "trip.internal": ("Interne fout bij het vastleggen van de rit.",
                      "Internal error while logging the trip."),

    # --- export ---------------------------------------------------------------
    "export.no_trips": ("Nog geen ritten vastgelegd voor {label}.", "No trips recorded yet for {label}."),
    "export.could_not": ("Exporteren lukte niet: {error}", "Could not export: {error}"),
    "export.generating": ("Je rittenoverzicht wordt gemaakt…", "Generating your spreadsheet…"),
    "export.failed": ("Export {year} mislukt: {error}", "Export {year} failed: {error}"),
    "export.failed_internal": ("Export {year} mislukt: interne fout.", "Export {year} failed: internal error."),
    "export.upload_failed": ("Export {year} is klaar, maar uploaden naar WhatsApp lukte niet.",
                             "Export {year} is ready but the upload to WhatsApp failed."),
    "export.not_sent": ("Export {year} is klaar, maar kon niet worden verstuurd.",
                        "Export {year} is ready but could not be sent."),
    "export.caption": ("{label} — {year} — {rows} ritten",
                       "{label} — {year} — {rows} trips"),
    "export.plugins": (" (bewerkt door {n} plugin{s})", " (processed by {n} plugin{s})"),

    # --- onboarding / unknown numbers ----------------------------------------------
    "onboard.signup": ("Hoi! Dit is RittenRegistratie-bot, je rittenregistratie via WhatsApp. "
                       "Dit nummer is nog niet aangemeld. Probeer het 14 dagen gratis: {url}",
                       "Hi! This is RittenRegistratie-bot, your trip log on WhatsApp. This number "
                       "isn't signed up yet. Try it free for 14 days: {url}"),
    "onboard.pending": ("Je verzoek om mee te doen wacht nog op goedkeuring van de beheerder.",
                        "Your request to join is still pending admin approval. Hang tight!"),
    "onboard.requested": ("Hoi! Dit is RittenRegistratie-bot. Je nummer is nog niet geregistreerd. "
                          "Ik heb de beheerder een verzoek gestuurd; je krijgt bericht als je bent "
                          "goedgekeurd.",
                          "Hi! This is RittenRegistratie-bot. Your number isn't registered yet. "
                          "I've sent a join request to the admin — you'll get a message when "
                          "you're approved."),
    "onboard.approved": ("Je bent goedgekeurd! Stuur '<kilometerstand> <bestemming>' om een rit "
                         "vast te leggen, of 'ping' om te testen. Je eerste bericht is het "
                         "beginpunt van je auto.",
                         "You're approved! Send '<odometer> <destination>' to log a trip, or "
                         "'ping' to test. First message sets your car's start."),

    # --- admin (to the owner, in the owner's language) ------------------------------
    "admin.join_request": ("Nieuw verzoek van {sender}: \"{message}\".\n"
                           "Goedkeuren: approve {sender} <label> <beginstand> [adres]\n"
                           "Of: deny {sender}",
                           "New join request from {sender}: \"{message}\".\n"
                           "Approve: approve {sender} <label> <seed_odo> [address]\n"
                           "Or: deny {sender}"),
    "admin.failed": ("Beheeropdracht mislukt.", "Admin command failed."),
    "admin.help": (
        "Beheeropdrachten:\n"
        "pending — openstaande verzoeken\n"
        "approve <nummer> <label> <beginstand> [adres...] — auto toevoegen voor een nummer "
        "(een nummer mag meerdere auto's hebben)\n"
        "assign <nummer> <car_id> — nummer laten rijden voor een bestaande auto\n"
        "unassign <nummer> <car_id> — assign ongedaan maken\n"
        "deny <nummer> — verzoek afwijzen\n"
        "list — auto's, nummers en plugins\n"
        "remove <nummer|car_id> — auto verwijderen, of een nummer van alle auto's\n"
        "stats [dagen] — statistieken (Engels)\n"
        "Iedereen: cars — je auto's; car <id> — actieve auto kiezen; "
        "excel [auto] [jaar|all] — rittenoverzicht.",
        "Admin commands:\n"
        "pending — list join requests\n"
        "approve <number> <label> <seed_odo> [address...] — add a car "
        "for a number (a number may have several cars)\n"
        "assign <number> <car_id> — let a number report for an existing car\n"
        "unassign <number> <car_id> — undo assign\n"
        "deny <number> — reject a request\n"
        "list — list cars, numbers and plugins\n"
        "remove <number|car_id> — remove a car, or a number from all cars\n"
        "stats [days] — statistics\n"
        "Everyone: cars — list your cars; car <id> — choose the active one; "
        "excel [car] [year|all] — spreadsheet."),
    "admin.list": ("Geregistreerde auto's ({n}/{max}):", "Registered cars ({n}/{max}):"),
    "admin.no_numbers": ("geen nummers", "no numbers"),
    "admin.list_line": ("- {label} [{id}] {phones} (beginstand {seed} km){extra}",
                        "- {label} [{id}] {phones} (seed {seed} km){extra}"),
    "admin.remove_usage": ("Gebruik: remove <nummer|car_id>", "Usage: remove <number|car_id>"),
    "admin.remove_none": ("Geen auto of nummer gevonden voor '{what}'.",
                          "No car or number found for '{what}'."),
    "admin.removed_cars": ("Auto('s) {cars} verwijderd. Hun rittenlog en status blijven op schijf.",
                           "Removed car(s) {cars}. Their ledger and state files are kept on disk."),
    "admin.detached": ("Nummer {phone} rijdt niet meer voor {cars}.",
                       "Number {phone} no longer reports for {cars}."),
    "admin.assign_usage": ("Gebruik: {cmd} <nummer> <car_id>", "Usage: {cmd} <number> <car_id>"),
    "admin.assign_failed": ("{cmd} lukte niet: {error}", "Could not {cmd}: {error}"),
    "admin.assigned": ("{phone} rijdt nu voor '{car}'.", "{phone} now reports for '{car}'."),
    "admin.already_assigned": ("{phone} rijdt al voor '{car}'.", "{phone} already reports for '{car}'."),
    "admin.unassigned": ("{phone} rijdt niet meer voor '{car}'.", "{phone} no longer reports for '{car}'."),
    "admin.not_assigned": ("{phone} was niet gekoppeld aan '{car}'.", "{phone} was not assigned to '{car}'."),
    "admin.pending": ("Openstaande verzoeken:", "Pending join requests:"),
    "admin.no_pending": ("Geen openstaande verzoeken.", "No pending join requests."),
    "admin.pending_hint": ("\nGoedkeuren: approve <nummer> <label> <beginstand> [adres]",
                           "\nApprove: approve <number> <label> <seed_odo> [address]"),
    "admin.deny_usage": ("Gebruik: deny <nummer>", "Usage: deny <number>"),
    "admin.denied": ("Verzoek van {phone} afgewezen en verwijderd.",
                     "Denied and removed request from {phone}."),
    "admin.no_request": ("Geen openstaand verzoek van {phone}.", "No pending request from {phone}."),
    "admin.approve_usage": ("Gebruik: approve <nummer> <label> <beginstand> [adres...]\n"
                            "bijv. approve 31612345678 \"Alice Golf\" 45000 Thuis",
                            "Usage: approve <number> <label> <seed_odo> [address...]\n"
                            "e.g. approve 31612345678 \"Alice Golf\" 45000 Home"),
    "admin.no_seed": ("Geen beginstand gevonden (een getal). Gebruik: approve <nummer> <label> "
                      "<beginstand> [adres]",
                      "Could not find the seed odometer (a number). Usage: approve <number> "
                      "<label> <seed_odo> [address]"),
    "admin.full": ("Maximum aantal gebruikers bereikt ({max}). Verwijder eerst iemand "
                   "(remove <nummer>) of verhoog RIT_MAX_USERS.",
                   "User limit reached ({max}). Remove a user first (remove <number>) or raise "
                   "RIT_MAX_USERS."),
    "admin.add_failed": ("Toevoegen lukte niet: {error}", "Could not add: {error}"),
    "admin.approved": ("{phone} goedgekeurd als '{label}' (car id: {id}, beginstand {seed} km bij "
                       "{address}).",
                       "Approved {phone} as '{label}' (car id: {id}, seed {seed} km at {address})."),
    "admin.unknown": ("Onbekende opdracht. Stuur 'help'.", "Unknown command. Send 'help'."),

    # --- subscription (customers) -------------------------------------------------
    "plan.monthly": ("maandelijks", "monthly"),
    "plan.yearly": ("jaarlijks", "yearly"),
    "unit.monthly": ("maand", "month"),
    "unit.yearly": ("jaar", "year"),
    "price": ("€ {amount} per {unit}", "€ {amount} per {unit}"),
    "support.at": (" op {email}", " at {email}"),
    "support.via": (" via {email}", " at {email}"),
    "sub.failed": ("Dat lukte nu niet. Probeer het later nog eens, of mail ons.",
                   "That didn't work right now. Please try again later, or email us."),
    "menu.range": ("Kies een nummer van 1 tot en met {n}, of stuur 'abonnement'.",
                   "Pick a number from 1 to {n}, or send 'subscription'."),
    "sub.already_plan": ("Je hebt al een {plan} abonnement. Stuur 'abonnement' voor het menu.",
                         "You already have a {plan} subscription. Send 'subscription' for the menu."),
    "sub.running": ("Je abonnement loopt al. Stuur 'abonnement' voor het menu.",
                    "Your subscription is already running. Send 'subscription' for the menu."),
    "sub.already_paused": ("Je abonnement is al gepauzeerd. Stuur 'hervatten' om verder te gaan.",
                           "Your subscription is already paused. Send 'resume' to continue."),
    "sub.already_cancelled": ("Je abonnement is al opgezegd; de bot werkt tot en met {date}. Stuur "
                              "'opnieuw' om toch door te gaan.",
                              "Your subscription is already cancelled; the bot works until {date}. "
                              "Send 'restart' to continue after all."),
    "sub.already_ended": ("Je abonnement is al afgelopen. Stuur 'opnieuw' om weer te beginnen.",
                          "Your subscription has already ended. Send 'restart' to start again."),
    "menu.active": ("Je abonnement is actief: {plan}, {price}.",
                    "Your subscription is active: {plan}, {price}."),
    "menu.trial": ("Proefperiode tot en met {date}.", "Free trial until {date}."),
    "menu.mollie": ("Let op: Mollie meldt de status '{status}'. We nemen contact op.",
                    "Note: Mollie reports the status '{status}'. We'll be in touch."),
    "menu.next": ("Volgende betaling: {date}, {amount}.", "Next payment: {date}, {amount}."),
    "menu.method": ("Betaalwijze: {method}.", "Payment method: {method}."),
    "menu.paused": ("Je abonnement is gepauzeerd ({plan}). Er wordt niets afgeschreven.",
                    "Your subscription is paused ({plan}). Nothing is charged."),
    "menu.paused_usable": ("Je kunt de bot gewoon gebruiken tot en met {date}.",
                           "You can keep using the bot until {date}."),
    "menu.paused_closed": ("Nieuwe ritten registreer ik pas weer na het hervatten; 'excel' werkt wel.",
                           "I'll log new trips again once you resume; 'excel' still works."),
    "menu.cancelled": ("Je abonnement is opgezegd; er wordt niets meer afgeschreven.",
                       "Your subscription is cancelled; nothing more will be charged."),
    "menu.works_until": ("De bot werkt tot en met {date}.", "The bot works until {date}."),
    "menu.ended": ("Je abonnement is afgelopen. Je ritten zijn bewaard.",
                   "Your subscription has ended. Your trips are kept."),
    "menu.choose": ("Stuur het nummer van je keuze:", "Reply with the number of your choice:"),
    "opt.switch": ("Overstappen naar {plan} ({price})", "Switch to {plan} ({price})"),
    "opt.pause": ("Pauzeren", "Pause"),
    "opt.cancel_final": ("Definitief opzeggen", "Cancel for good"),
    "opt.cancel": ("Opzeggen", "Cancel"),
    "opt.resume": ("Hervatten, {plan} ({price})", "Resume, {plan} ({price})"),
    "opt.restart": ("Opnieuw starten, {plan} ({price})", "Restart, {plan} ({price})"),
    "sub.cancelled": ("Je abonnement is opgezegd. Er wordt niets meer afgeschreven. De bot werkt "
                      "tot en met {date}. Stuur vóór die datum 'excel all' om al je ritten als "
                      "Excel te bewaren. Bedacht? Stuur 'opnieuw'.",
                      "Your subscription is cancelled. Nothing more will be charged. The bot works "
                      "until {date}. Send 'excel all' before then to keep all your trips as Excel. "
                      "Changed your mind? Send 'restart'."),
    "sub.paused": ("Je abonnement is gepauzeerd. Er wordt niets meer afgeschreven. Je kunt de bot "
                   "gewoon gebruiken tot en met {date}; daarna registreer ik geen nieuwe ritten tot "
                   "je 'hervatten' stuurt. Je ritten blijven bewaard en 'excel' werkt altijd.",
                   "Your subscription is paused. Nothing more will be charged. You can keep using "
                   "the bot until {date}; after that I won't log new trips until you send "
                   "'resume'. Your trips are kept and 'excel' always works."),
    "sub.switch_no_mandate": ("Je betaalmachtiging is niet meer geldig, dus overstappen lukt nu niet. "
                              "Mail ons{support}.",
                              "Your payment mandate is no longer valid, so switching doesn't work "
                              "right now. Email us{support}."),
    "sub.switch_failed": ("Overstappen lukte niet helemaal. Je abonnement staat even op pauze; je "
                          "kunt de bot gewoon blijven gebruiken. We nemen contact op{support}.",
                          "The switch didn't fully go through. Your subscription is paused for "
                          "now; you can keep using the bot. We'll contact you{support}."),
    "sub.switched": ("Je stapt over naar {plan}. Je huidige periode loopt gewoon door; vanaf "
                     "{date} wordt {price} afgeschreven.",
                     "You're switching to {plan}. Your current period carries on; from {date} "
                     "you'll be charged {price}."),
    "sub.cancelled_paused": ("Je abonnement is opgezegd. De bot werkt tot en met {date}. Stuur vóór "
                             "die datum 'excel all' om al je ritten als Excel te bewaren.",
                             "Your subscription is cancelled. The bot works until {date}. Send "
                             "'excel all' before then to keep all your trips as Excel."),
    "sub.cancelled_paused_now": ("Je abonnement is opgezegd. Stuur nu 'excel all' als je je ritten "
                                 "nog als Excel wilt hebben; bij je volgende bericht daarna haal ik "
                                 "je nummer van de bot.",
                                 "Your subscription is cancelled. Send 'excel all' now if you want "
                                 "your trips as Excel; with your next message after that I'll "
                                 "remove your number from the bot."),
    "sub.renew_link": ("Je betaalmachtiging is niet meer geldig. Rond via deze link een betaling van "
                       "€ {amount} af om hem te vernieuwen; daarna loopt je {plan} abonnement "
                       "({price}) meteen weer:\n{url}",
                       "Your payment mandate is no longer valid. Complete a payment of € {amount} "
                       "through this link to renew it; your {plan} subscription ({price}) then "
                       "runs again straight away:\n{url}"),
    "when.today": ("vandaag", "today"),
    "when.on": ("op {date}", "on {date}"),
    "sub.running_again": ("Je abonnement loopt weer: {plan}, {price}. De eerste betaling is {when}, "
                          "via {method}. Stuur je kilometerstand en bestemming zoals altijd.",
                          "Your subscription is running again: {plan}, {price}. The first payment "
                          "is {when}, by {method}. Send your odometer and destination as usual."),
    "sub.running_again_short": ("Je abonnement loopt weer: {plan}, {price}. Stuur je kilometerstand "
                                "en bestemming zoals altijd.",
                                "Your subscription is running again: {plan}, {price}. Send your "
                                "odometer and destination as usual."),
    "sub.other_car": ("Dit nummer is al aan een andere auto gekoppeld. Mail ons{support}.",
                      "This number is already linked to another car. Email us{support}."),
    "sub.car_blocked": ("Je auto kan niet automatisch terugkomen. Mail ons{support}.",
                        "Your car can't come back automatically. Email us{support}."),
    "sub.full": ("We zitten op dit moment vol. Probeer het later opnieuw.",
                 "We're full at the moment. Please try again later."),
    "sub.manual": ("Je abonnement loopt weer. We zetten je nummer handmatig terug en sturen je "
                   "binnen een werkdag een bericht.",
                   "Your subscription is running again. We'll put your number back by hand and "
                   "message you within one working day."),
    "sub.ended_deleted": ("Je abonnement is afgelopen en je ritten zijn na de bewaartermijn "
                          "verwijderd. Stuur 'opnieuw' om opnieuw te beginnen, of 'abonnement' voor "
                          "de opties.",
                          "Your subscription has ended and your trips were deleted after the "
                          "retention period. Send 'restart' to start again, or 'subscription' for "
                          "the options."),
    "sub.ended": ("Je abonnement is afgelopen, dus ik registreer geen ritten meer. Je ritten worden "
                  "nog {days} dagen bewaard: stuur 'opnieuw' om verder te gaan, of vraag een "
                  "kopie{support}. 'abonnement' toont de opties.",
                  "Your subscription has ended, so I no longer log trips. Your trips are kept for "
                  "{days} more days: send 'restart' to continue, or ask for a copy{support}. "
                  "'subscription' shows the options."),
    "sub.paused_block": ("Je abonnement is gepauzeerd, dus ik registreer geen nieuwe ritten. Stuur "
                         "'hervatten' om verder te gaan, of 'abonnement' voor het menu. 'excel' werkt "
                         "gewoon.",
                         "Your subscription is paused, so I don't log new trips. Send 'resume' to "
                         "continue, or 'subscription' for the menu. 'excel' still works."),
    "sub.none": ("Ik vind geen abonnement bij dit nummer.", "I can't find a subscription for this number."),
    "sub.none_signup": ("Ik vind geen abonnement bij dit nummer. Aanmelden: {url}",
                        "I can't find a subscription for this number. Sign up: {url}"),
    "sub.attention": ("Je aanmelding wordt nog afgerond; we zetten je nummer handmatig aan en sturen "
                      "je binnen een werkdag een bericht.",
                      "Your signup is still being completed; we'll switch your number on by hand "
                      "and message you within one working day."),
    "sub.charge_day": ("Vandaag wordt je volgende termijn afgeschreven, dus dit kan vandaag niet. "
                       "Probeer het morgen nog eens.",
                       "Your next instalment is charged today, so this can't be done today. "
                       "Please try again tomorrow."),
    "mandate.none": ("geen geldige machtiging", "no valid mandate"),
    "mandate.sepa": ("SEPA-incasso", "SEPA direct debit"),
    "mandate.sepa_tail": ("SEPA-incasso van rekening eindigend op {tail}",
                          "SEPA direct debit from the account ending in {tail}"),
    "mandate.card": ("creditcard", "credit card"),
    "mandate.card_tail": ("{label} eindigend op {tail}", "{label} ending in {tail}"),
    "mandate.unknown": ("onbekend", "unknown"),
    "mandate.pending": (" (wordt nog verwerkt)", " (still being processed)"),

    # --- invoices (invoices.py) -------------------------------------------------------
    "inv.caption": ("Factuur {number}: € {amount}, {start} t/m {end}.",
                    "Invoice {number}: € {amount}, {start} to {end}."),
    "inv.none": ("Er is nog geen factuur voor dit nummer. De eerste komt na de eerste betaling van "
                 "je abonnement, na de proefperiode.",
                 "There is no invoice for this number yet. The first one comes after the first "
                 "payment of your subscription, after the trial."),
    "inv.not_found": ("Factuur {number} vind ik niet bij dit nummer. Stuur 'facturen' voor je facturen.",
                      "I can't find invoice {number} for this number. Send 'invoices' for your "
                      "invoices."),
    "inv.list": ("Je facturen:", "Your invoices:"),
    "inv.list_hint": ("Stuur 'factuur <nummer>' voor de pdf.", "Send 'invoice <number>' for the PDF."),
    "inv.details": ("Factuurgegevens\nNaam: {name}\nBedrijfsnaam: {company}\nFactuuradres: {address}\n"
                    "Wijzigen: 'bedrijfsnaam <naam>' of 'factuuradres <straat 1, 1234 AB plaats>'; "
                    "'-' wist het. Nieuwe gegevens gelden vanaf de volgende factuur.",
                    "Billing details\nName: {name}\nCompany: {company}\nBilling address: {address}\n"
                    "Change: 'company <name>' or 'billing address <street 1, 1234 AB town>'; "
                    "'-' clears it. New details apply from the next invoice."),
    "inv.none_set": ("(niet ingevuld)", "(not set)"),
    "inv.saved": ("Opgeslagen.", "Saved."),
    "inv.not_sent": ("De factuur kon niet worden verstuurd. Probeer het later opnieuw.",
                     "The invoice could not be sent. Please try again later."),

    # --- event types: the first parameter of the subscriber_event template ----------
    "event.new": ("nieuwe abonnee", "new subscriber"),
    "event.provision_failed": ("inrichten mislukt", "setup failed"),
    "event.charge_failed": ("betaling mislukt", "payment failed"),
    "event.cancelled": ("opzegging", "cancellation"),
    "event.paused": ("pauze", "pause"),
    "event.switch_failed": ("overstap mislukt", "plan change failed"),
    "event.switched": ("overstap", "plan change"),
    "event.cancelled_paused": ("opzegging", "cancellation"),
    "event.no_mandate": ("machtiging verlopen", "mandate expired"),
    "event.continues": ("hervatting", "restart"),
    "event.reinstate_failed": ("hervatten mislukt", "restart failed"),
    "event.renewed": ("hervatting", "restart"),
    "event.join_request": ("aanmeldverzoek", "join request"),
    "event.other": ("melding", "notice"),
    "event.invoice_failed": ("factuur mislukt", "invoice failed"),
    "event.wa_quality": ("WhatsApp-kwaliteit", "WhatsApp quality"),
    "event.wa_tier": ("WhatsApp-berichtlimiet", "WhatsApp messaging limit"),

    # --- alerts to admins (in each admin's language) ----------------------------------
    "alert.provision_failed": ("Betaalde aanmelding {id} ({phone}, {car}) kon niet worden ingericht: "
                               "{error}. Abonnement {sub} loopt.",
                               "Paid signup {id} ({phone}, {car}) could not be set up: {error}. "
                               "Subscription {sub} is running."),
    "alert.new": ("Nieuwe abonnee: {name} ({phone}), {car}, {plan}, proef tot {trial}. Auto: {car_id}.",
                  "New subscriber: {name} ({phone}), {car}, {plan}, trial until {trial}. Car: {car_id}."),
    "alert.charge_failed": ("Incasso {payment} van {name} ({phone}, {car}) is {status}. Nummer blijft "
                            "actief; beslis zelf (verwijderen: 'remove {phone}').",
                            "Charge {payment} for {name} ({phone}, {car}) is {status}. The number "
                            "stays active; your call (remove: 'remove {phone}')."),
    "alert.cancelled": ("{name} ({phone}) heeft opgezegd, actief t/m {date}.",
                        "{name} ({phone}) cancelled, active until {date}."),
    "alert.paused": ("{name} ({phone}, {car}) heeft gepauzeerd, betaald t/m {date}.",
                     "{name} ({phone}, {car}) paused, paid until {date}."),
    "alert.switch_failed": ("Overstap van {name} ({phone}) naar {plan} mislukte na het opzeggen van "
                            "{old}; staat nu op pauze. Kijk in Mollie.",
                            "{name} ({phone})'s switch to {plan} failed after cancelling {old}; "
                            "now paused. Check Mollie."),
    "alert.switched": ("{name} ({phone}) stapt over naar {plan} per {date}.",
                       "{name} ({phone}) switches to {plan} from {date}."),
    "alert.cancelled_paused": ("{name} ({phone}) heeft tijdens de pauze opgezegd, toegang t/m {date}.",
                               "{name} ({phone}) cancelled while paused, access until {date}."),
    "alert.no_mandate": ("{name} ({phone}) wil verder ({plan}) maar heeft geen geldige machtiging; "
                         "betaallink gestuurd.",
                         "{name} ({phone}) wants to continue ({plan}) but has no valid mandate; "
                         "payment link sent."),
    "alert.continues": ("{name} ({phone}, {car}) gaat verder na {state}: {plan} vanaf {date}.",
                        "{name} ({phone}, {car}) continues after {state}: {plan} from {date}."),
    "state.paused": ("pauze", "a pause"),
    "state.canceled": ("opzegging", "cancelling"),
    "state.ended": ("einde abonnement", "the subscription ended"),
    "alert.reinstate_failed": ("{name} ({phone}) betaalt weer, maar auto {car} kon niet terug in "
                               "cars.yaml: {error}. Abonnement {sub} loopt.",
                               "{name} ({phone}) is paying again, but car {car} could not go back "
                               "into cars.yaml: {error}. Subscription {sub} is running."),
    "alert.invoice_failed": ("Betaling {payment} van {name} ({phone}) is binnen, maar de factuur kon "
                             "niet worden gemaakt. Zie de log; opnieuw: "
                             "'python -m rittenregistratie.invoices issue {payment}'.",
                             "Payment {payment} from {name} ({phone}) is in, but the invoice could "
                             "not be made. See the log; retry: "
                             "'python -m rittenregistratie.invoices issue {payment}'."),
    "alert.wa_quality": ("De kwaliteit van het botnummer bij Meta is gedaald van {old} naar {new}. Bij "
                         "RED beperkt Meta het nummer; kijk in WhatsApp Manager wat ontvangers "
                         "blokkeren of melden.",
                         "Meta's quality rating of the bot number dropped from {old} to {new}. At "
                         "RED Meta restricts the number; check WhatsApp Manager for blocks and "
                         "reports."),
    "alert.wa_tier": ("De berichtlimiet van het botnummer is veranderd van {old} naar {new} "
                      "(gesprekken die de bot per 24 uur mag beginnen).",
                      "The bot number's messaging limit changed from {old} to {new} "
                      "(conversations the bot may start per 24 hours)."),
    "alert.renewed": ("{name} ({phone}, {car}) heeft een nieuwe machtiging en gaat verder: {plan} "
                      "vanaf {date}.",
                      "{name} ({phone}, {car}) has a new mandate and continues: {plan} from {date}."),
}
