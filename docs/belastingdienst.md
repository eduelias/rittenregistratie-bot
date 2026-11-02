# Belastingdienst rittenregistratie fields

To keep a company car out of the taxable *bijtelling* using the
**Verklaring geen privégebruik auto**, you must not drive more than **500 km
privately per year** and you must be able to prove it with a **sluitende
rittenregistratie** (watertight trip log).

Per the official
[rittenregistratie requirements](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/winst/inkomstenbelasting/inkomstenbelasting_voor_ondernemers/privegebruik_auto/rittenregistratie),
each trip (**per rit**) must contain exactly these fields — the generated Excel
has one column per field:

| Excel column | Official field |
|---|---|
| Datum | de datum |
| Beginstand | de beginstand van de kilometerteller |
| Eindstand | de eindstand van de kilometerteller |
| Vertrekadres | het vertrekadres |
| Aankomstadres | het aankomstadres |
| Route | de route die u hebt gereden, **als u niet de meest gebruikelijke route hebt genomen** (empty otherwise) |
| Privé/zakelijk | of het een privérit is of een zakelijke rit |
| Privé-omrijkilometers | de privé-omrijkilometers als tijdens een rit zowel zakelijke als privékilometers worden gereden |

Note: a there-and-back visit counts as **2 trips** (heenreis + terugreis), each
its own row.

For employees and DGAs with a company car, the employer manual
([Handboek Loonheffingen 2026](https://download.belastingdienst.nl/belastingdienst/docs/handboek-loonheffingen-lh0221t62fd.pdf),
§23.3.18) also asks for a **trip number per day** and, when several people
drive the car, **who drove**. The export ends with these two columns, the same
as the free template on rrbot.nl/rittenregistratie-excel:

| Excel column | Filled with |
|---|---|
| Ritnummer | 1, 2, 3 … per date, in the order the trips were logged |
| Bestuurder | only when the car has more than one phone in `cars.yaml`: the name from that car's `drivers:` map, else the phone number that reported the trip |

Bestuurder comes from the ledger's `sender` field, recorded since this column
was added. Trips logged before that, and trips the car logged itself (vehicle
telemetry), have no sender, so their Bestuurder stays empty rather than being
guessed. A car with one phone never fills it.

The public guide rrbot.nl/rittenregistratie-eisen
(`src/rittenregistratie/site/rittenregistratie-eisen.html`) summarises the rules
with a source link per rule, checked on 2026-11-02.

> Always verify the current, exact requirements on the official Belastingdienst
> website before relying on any trip log for a declaration. This document is a
> convenience summary, not tax advice.

Official page:
<https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/winst/inkomstenbelasting/inkomstenbelasting_voor_ondernemers/privegebruik_auto/rittenregistratie>
