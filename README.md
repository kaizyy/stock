# Stockroom

Een lichte voorraadapp voor inkomende en uitgaande artikelen, met prijsregistratie, betaalstatus en leverstatus.

## Deployen met Coolify

1. Maak in Coolify een nieuwe resource aan vanuit deze GitHub-repository.
2. Kies **Dockerfile** als build pack.
3. Gebruik `Dockerfile` als Dockerfile-locatie en `/` als build context.
4. Stel containerpoort **80** in.
5. Voeg je domein toe en start de deployment.

De container bevat een healthcheck op `/health`. Er zijn geen environment variables nodig.

## Lokaal draaien

```bash
docker build -t stockroom .
docker run --rm -p 8080:80 stockroom
```

Open daarna `http://localhost:8080`.

