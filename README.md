# Stockroom

Een lichte voorraadapp voor inkomende en uitgaande artikelen, met prijsregistratie, betaalstatus en leverstatus.

## Deployen met Coolify

1. Maak in Coolify een nieuwe resource aan vanuit deze GitHub-repository.
2. Kies **Dockerfile** als build pack.
3. Gebruik `Dockerfile` als Dockerfile-locatie en `/` als build context.
4. Stel containerpoort **80** in.
5. Voeg onder **Environment Variables** de volgende verplichte waarden toe:
   - `STOCKROOM_USERNAME`: de gewenste gebruikersnaam.
   - `STOCKROOM_PASSWORD`: een sterk, uniek wachtwoord. Markeer deze als secret.
6. Voeg je domein toe en start de deployment.

De volledige website, inclusief CSS en JavaScript, is beveiligd met HTTP Basic Authentication. Zonder geldige inloggegevens geeft de webserver geen website-inhoud terug. Alleen `/health` blijft zonder authenticatie beschikbaar voor de technische controle van Coolify; dit endpoint toont uitsluitend `healthy`.

De container start bewust niet als een van de twee inlogvariabelen ontbreekt.

## Lokaal draaien

```bash
docker build -t stockroom .
docker run --rm -p 8080:80 \
  -e STOCKROOM_USERNAME=beheerder \
  -e STOCKROOM_PASSWORD='kies-een-sterk-wachtwoord' \
  stockroom
```

Open daarna `http://localhost:8080`.

