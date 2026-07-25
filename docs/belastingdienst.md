# Belastingdienst rittenregistratie fields

To keep a company car out of the taxable *bijtelling* using the
**Verklaring geen privégebruik auto**, you must not drive more than **500 km
privately per year** and you must be able to prove it with a **sluitende
rittenregistratie** (watertight trip log).

Per trip, the administration generally must contain:

- **Date** of the trip.
- **Begin and end odometer** reading (from which the trip distance follows).
- **Address of departure and arrival**.
- **The route actually driven**, if it deviates from the most common route.
- The **business or private character** of the trip (and, for private trips,
  the private kilometres).

This tool records all of these fields (see the Excel schema in the README). The
`DeviationNote` column is filled automatically when the measured distance does
not match a known route variant, which is where you should describe the actual
route taken.

> Always verify the current, exact requirements on the official Belastingdienst
> website before relying on any trip log for a declaration. This document is a
> convenience summary, not tax advice.

Official page:
<https://www.belastingdienst.nl/wps/wcm/connect/nl/personeel-en-loon/content/verklaring-geen-privegebruik-auto-aanvragen-wijzigen-intrekken>
