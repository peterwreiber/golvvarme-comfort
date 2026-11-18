# Golvvärme Comfort

Home Assistant-tillägg som styr elektrisk golvvärme efter elpriset. Du anger när golvet ska vara varmt, och tillägget förvärmer under de billigaste kvartarna innan och sänker när elen är dyr.

## Funktioner

- **Komforttider** per veckodag (24-timmarsklocka). Övrig tid hålls en ekonomitemperatur.
- **Förvärmning** inför varje komforttid, med start när det blir billigast. Bygger på golvets uppvärmnings- och avsvalningstakt.
- **Dyr el:** sänkt temperatur utanför komforttid och valfri liten sänkning under komforttid. Dyrt räknas antingen relativt (dygnets dyraste del) eller över ett fast pris. Dyra eller billiga perioder kortare än en timme räknas inte, eftersom golvet håller värmen längre än så och varje ändring kostar anrop till termostaten.
- **Dyr förvärmning:** måste golvet värmas under dygnets dyra timmar för att nå komforttiden, sänks komfortmålet med samma steg som vid dyr el. Förvärmningen blir då kortare. Kan stängas av.
- **Närvaro (valfri):** välj en person, zon, grupp eller närvarogivare. Är ingen hemma (efter en marginal, standard 15 minuter) förvärms inte golvet och komforttiden blir ekonomi. Efter lång bortavaro (standard 4 timmar) gäller sänkt temperatur. Närvaron kan bara sänka: ett golv tar 1–3 timmar att värma, så när någon kommer hem gäller planen direkt men golvet blir varmt först efter en stund. Okänd eller otillgänglig närvaro räknas som hemma.
- **Nära hemmet (valfri, km):** ett golv hinner inte värmas under resan hem. Är någon i hushållet inom så många km från hemmet (enligt mobilens GPS i Home Assistants personer) räknas huset som bebott och vanlig plan med förvärmning gäller. Då behövs ingen jobbzon, och det fungerar även om jobbet byter plats. Närvarostyrningen sparar då på resor och längre bortavaro. 0 = av.
- **Billig el:** valfri extra värme under ett visst pris.
- **Semesterläge:** komforttiderna pausas och golvet hålls på en semestertemperatur. Före hemkomsten förvärms golvet, och semesterläget stängs av automatiskt efteråt.
- **Elpris från Tibber eller Nord Pool**, både den officiella integrationen och HACS-varianten.
- **Fungerar med de flesta termostater i HA.** Tillägget anpassar sig efter termostatens lägen (`heat`/`auto`), temperatursteg och gränser.
- **Säker reserv:** stängs styrningen av, stoppas tillägget eller saknas elpriset, går termostaten tillbaka till sitt eget program. Saknar den eget program ställs en reservtemperatur in.
- Webbgränssnitt i sidomenyn med plan, graf, historik och uppskattad kostnad. Ljust och mörkt tema.
- Sensorerna `sensor.golvvarme_comfort_mal` och `sensor.golvvarme_comfort_status` för egna kort och automationer.

## Installation

1. I Home Assistant: **Inställningar → Tillägg → Tilläggsbutik → ⋮ → Förråd** och lägg till `https://github.com/peterwreiber/golvvarme-comfort`.
2. Installera **Golvvärme Comfort** och starta det.
3. Öppna **Golvvärme** i sidomenyn. Under **Avancerat** väljer du termostat och elprisgivare.
4. Låt det gå i läget "bara plan" ett tag och kontrollera planen. Slå sedan på **Styr termostaten**.

Har du en egen automation som redan styr termostaten efter elpriset kan du ange den under Avancerat. Tillägget varnar då och kan stänga av den.

## Tips

- **Uppvärmningstakt** och **avsvalning** (°C/h) avgör när förvärmningen börjar. Med **Använd inlärda takter** mäts de ur termostatens egen historik (värmer/vilar, senaste 10 dygnen) och ersätter de inskrivna värdena när det finns minst tre av varje. Är golvet kallt när komforttiden börjar, sänk uppvärmningstakten eller höj längsta förvärmning.
- Den officiella Nord Pool-integrationen ger spotpris utan skatt och nätavgift. Sätt därför gränsen för dyr el lägre än för Tibber.
- Effekten (kW) används bara för att uppskatta kostnaden.

## Utveckling

```sh
cd golvvarme_comfort
python -m unittest discover -s tests
```

Bara Pythons standardbibliotek används. Planen räknas ut i `planner.py`, priserna hämtas i `prices.py`, och styrning och webbserver finns i `app.py`.
