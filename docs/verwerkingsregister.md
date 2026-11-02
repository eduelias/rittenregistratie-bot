# Verwerkingsregister RittenRegistratie-bot

Register van verwerkingsactiviteiten (art. 30 AVG). Bijgewerkt op 2 november
2026. Bijwerken bij elke nieuwe verwerking, verwerker of bewaartermijn, en
tegelijk de privacyverklaring (`src/rittenregistratie/site/privacy.html`)
nalopen.

## Verwerkingsverantwoordelijke

Du7 Technology Services, Buitenhof 128, 1354 GT Almere. KvK 91476003, btw
NL004896802B29. Contact: support@rrbot.nl. Er is geen functionaris
gegevensbescherming: dat is niet verplicht voor deze verwerking.

## Betrokkenen

- **Klanten**: abonnees van RittenRegistratie-bot (zzp'ers, ondernemers,
  werknemers met een auto van de zaak) en wie zich via rrbot.nl aanmeldt.
- **Bezoekers** van rrbot.nl.
- **Beheerder**: de eigenaar zelf (eigen auto's, beheerberichten).

## Verwerkingen

| # | Verwerking | Doel | Grondslag | Gegevens | Bewaartermijn |
|---|---|---|---|---|---|
| 1 | Aanmelding en abonnement | Klant aanmelden, abonnement starten, pauzeren, wijzigen en opzeggen | Overeenkomst | Naam, e-mailadres, WhatsApp-nummer, naam van de auto, beginstand, beginadres, gekozen abonnement, Mollie-klant-, betaal-, machtigings- en abonnementsnummers, statusgeschiedenis (`data/signups.json`) | 7 jaar na het einde van het abonnement (fiscale bewaarplicht, art. 52 AWR). Nog handmatig; de eerste klanten bereiken die termijn in 2033 |
| 2 | Rittenregistratie | De dienst zelf: ritten vastleggen en als Excel leveren | Overeenkomst | Kilometerstanden, datum en tijd, vertrek- en aankomstadressen, route, privé of zakelijk, opmerkingen, gedeelde locaties, geleerde adressen, wie de rit stuurde (`data/raw-ledger-*.jsonl`, `data/locations-learned-*.yaml`, `data/exports/`) | Looptijd van het abonnement plus `RIT_RETENTION_DAYS` (90) dagen, daarna automatisch verwijderd (`retention.py`, dagelijkse timer) |
| 3 | Berichten via WhatsApp | Met de klant communiceren: antwoorden, sjabloonberichten, opzegcode | Overeenkomst | WhatsApp-nummer, inhoud van berichten aan en van de bot, taalkeuze (`data/languages.json`), tijdstip van het laatste bericht (`data/last-inbound.json`), opzegcode (alleen gehasht in het geheugen, 10 minuten) | Taal en tijdstip als bij 2. Berichtinhoud wordt niet apart bewaard; wat een rit wordt, valt onder 2 |
| 4 | Betalingen | Machtiging en terugkerende betalingen | Overeenkomst | Via Mollie: naam, e-mailadres, betaalmethode, rekening- of kaartgegevens (alleen bij Mollie), betaalstatus | Bij ons als bij 1. Mollie bewaart zelf (eigen verantwoordelijkheid) |
| 5 | Facturen | Btw-factuur bij elke betaalde termijn (art. 35a Wet OB) | Wettelijke plicht | Factuurnummer, datum, naam, bedrijfsnaam en factuuradres (optioneel), e-mailadres, WhatsApp-nummer, omschrijving, periode, bedragen, Mollie-betaalnummer (`data/invoices/`) | 7 jaar (art. 52 AWR) |
| 6 | Websitestatistieken | Weten hoeveel bezoekers de site trekt en waar ze vandaan komen | Gerechtvaardigd belang | Geen persoonsgegevens: tellingen per dag per pagina, verwijzend domein, campagnecode. Geen IP-adressen, geen cookies (`data/analytics.json`) | Onbepaald (geen persoonsgegevens) |
| 7 | Serverlogs | Storingen en misbruik onderzoeken | Gerechtvaardigd belang | IP-adres, tijdstip, opgevraagde pagina, telefoonnummer bij een bezorgfout. Geen berichtinhoud | 30 dagen (journald `MaxRetentionSec=30day`) |
| 8 | Back-ups | Herstel na een storing | Gerechtvaardigd belang | Alles uit `data/` en `config/` (niet `.env`) | 30 dagen, daarna automatisch verwijderd (`backup.py --keep-days 30`) |
| 9 | Klantenservice per e-mail | Vragen beantwoorden | Overeenkomst, gerechtvaardigd belang | E-mailadres, inhoud van de mail (support@rrbot.nl, doorgestuurd door Cloudflare naar de eigen mailbox) | Zolang nodig voor de vraag; uiterlijk als bij 1 |

Geen geautomatiseerde besluitvorming, geen profilering, geen verkoop of
reclamegebruik. Bijzondere persoonsgegevens worden niet gevraagd; ritten
kunnen wel iets zeggen over waar iemand woont of werkt en worden daarom
vertrouwelijk behandeld.

## Ontvangers en verwerkers

| Partij | Rol | Wat | Verwerkersovereenkomst / voorwaarden | Doorgifte buiten de EU |
|---|---|---|---|---|
| WhatsApp Ireland Ltd. (Meta) | Verwerker | Bezorgt berichten, sjablonen en documenten (Excel, facturen) | [WhatsApp Business Data Processing Terms](https://www.whatsapp.com/legal/business-data-processing-terms) bij de [WhatsApp Business Platform-voorwaarden](https://www.whatsapp.com/legal/WhatsApp-Terms-for-WhatsApp-Business-Platform); gelden door gebruik, geen aparte klik | Ja, naar WhatsApp LLC en Meta Platforms in de VS: EU-VS Data Privacy Framework, standaardcontractbepalingen als terugval ([Data Transfer Addendum](https://www.whatsapp.com/legal/business-data-transfer-addendum)) |
| Mollie B.V., Amsterdam | Zelfstandig verwerkingsverantwoordelijke voor betalingen | Betalingen, machtigingen, abonnementen | Geen verwerkersovereenkomst nodig voor betalingen. De [Mollie DPA](https://www.mollie.com/legal/data-processing-agreement) is deel van de gebruikersovereenkomst en geldt alleen voor Mollie-diensten waarbij Mollie verwerker is (zoals Mollie Invoicing, niet in gebruik) | Binnen de EER; buiten alleen met passende waarborgen (DPA art. C.1). [Subverwerkers](https://www.mollie.com/legal/sub-processors) |
| Google Ireland Ltd. (Google Maps Platform) | Verwerker | Zet een gedeelde locatie om naar een adres, en routes als `RIT_TRAJECTORY_PROVIDER=google` | [Google Maps Platform EEA-voorwaarden](https://cloud.google.com/terms/maps-platform/eea) en de [Cloud Data Processing Addendum](https://cloud.google.com/terms/data-processing-addendum). EEA-voorwaarden moeten voor projecten met een EER-factuuraccount in de Cloud Console worden geaccepteerd (zie hieronder) | Mogelijk, waar Google faciliteiten heeft: standaardcontractbepalingen |
| Cloudflare, Inc. | Verwerker | DNS van rrbot.nl, doorsturen van mail aan support@rrbot.nl | [Cloudflare Customer DPA](https://www.cloudflare.com/cloudflare-customer-dpa/), automatisch deel van de [Self-Serve Subscription Agreement](https://www.cloudflare.com/terms/); geen actie nodig | Ja, VS en Europa: EU-VS Data Privacy Framework, standaardcontractbepalingen als terugval |
| Belastingdienst | Ontvanger op grond van de wet | Facturen en administratie bij een controle | n.v.t. | Nee |

Hosting en opslag gebeuren op een eigen server (Raspberry Pi `du7`) in
Nederland; de back-up staat op een aparte schijf in hetzelfde huis. Daar is
geen verwerker bij betrokken.

### Openstaand voor de eigenaar

- [ ] **Google Maps Platform**: in de Google Cloud Console, project van de
      Maps-sleutel, op de [Maps Platform Support-pagina](https://console.cloud.google.com/google/maps-apis/support)
      kijken of er een melding staat om de EEA-voorwaarden te accepteren, en
      zo ja accepteren (alleen voor projecten met een EER-factuuraccount
      aangemaakt op of na 8 juli 2025). Dat is een juridische acceptatie: de
      eigenaar doet dat zelf.
- [ ] Een pdf-kopie van elke overeenkomst hierboven bewaren bij de
      administratie, met de datum.
- [ ] Ritten van de eigen auto's van de eigenaar vallen buiten de
      klantverwerkingen, maar staan op dezelfde server.

## Beveiligingsmaatregelen (art. 32 AVG)

- De site is alleen via HTTPS bereikbaar (Caddy, HSTS); de server staat achter
  de router-firewall.
- Toegang tot de server alleen voor de eigenaar.
- Inkomende WhatsApp-berichten worden gecontroleerd met de handtekening van
  Meta; een Mollie-webhook bevat alleen een id, de status wordt altijd bij
  Mollie zelf opgehaald.
- Elke klant heeft een eigen rittenlog, eigen adresboek en geen plugins van de
  eigenaar.
- Geheimen (API-sleutels, tokens) staan in 1Password en in een `.env` op de
  server die niet in git of de back-up komt.
- Opzeggen via de website alleen met een code naar het WhatsApp-nummer van het
  abonnement; maximaal 5 pogingen, 3 codes per uur.
- Dagelijkse back-up; elk archief wordt na het schrijven gecontroleerd.

## Datalekken

Een datalek (bijvoorbeeld een gestolen of gehackte server, of een back-up op
de verkeerde plek) binnen 72 uur melden bij de [Autoriteit
Persoonsgegevens](https://www.autoriteitpersoonsgegevens.nl/), en de klanten
inlichten als het risico hoog is. Elk lek, ook een niet-gemeld, hier
vastleggen met datum, wat er gebeurde, de gevolgen en de maatregelen.

| Datum | Wat | Gemeld | Maatregelen |
|---|---|---|---|
| | | | |
