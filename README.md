# Stockroom

Een lichte voorraadapp voor inkomende en uitgaande artikelen, met prijsregistratie, betaalstatus en leverstatus.

## Deployen met Coolify

1. Maak in Coolify een nieuwe resource aan vanuit deze GitHub-repository.
2. Kies **Dockerfile** als build pack.
3. Gebruik `Dockerfile` als Dockerfile-locatie en `/` als build context.
4. Stel containerpoort **8000** in.
5. Voeg onder **Environment Variables** de volgende verplichte waarden toe:
   - `STOCKROOM_USERNAME`: de gewenste gebruikersnaam.
   - `STOCKROOM_PASSWORD`: een sterk, uniek wachtwoord. Markeer deze als secret.
6. Voeg onder **Persistent Storage** een volume toe met mount path `/data`.
7. Voeg je domein toe en start de deployment.

De volledige website, inclusief CSS en JavaScript, is beveiligd met HTTP Basic Authentication. Zonder geldige inloggegevens geeft de webserver geen website-inhoud terug. Alleen `/health` blijft zonder authenticatie beschikbaar voor de technische controle van Coolify; dit endpoint toont uitsluitend `healthy`.

De container start bewust niet als een van de twee inlogvariabelen ontbreekt. Voorraad en transacties worden centraal opgeslagen in `/data/stockroom.db`. Zolang `/data` als persistent volume is gekoppeld, blijven de gegevens behouden bij rebuilds, restarts en redeploys. De gegevens zijn bovendien beschikbaar vanaf ieder apparaat dat met de juiste inloggegevens toegang heeft.

## Lokaal draaien

```bash
docker build -t stockroom .
docker run --rm -p 8080:8000 \
  -e STOCKROOM_USERNAME=beheerder \
  -e STOCKROOM_PASSWORD='kies-een-sterk-wachtwoord' \
  -v stockroom-data:/data \
  stockroom
```

Open daarna `http://localhost:8080`.

