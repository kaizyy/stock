# Stockroom

Multi-tenant voorraadbeheer met PostgreSQL. Iedere nieuwe registratie krijgt een eigen, afgescheiden stockroom. Gebruikers kunnen daarnaast expliciet worden gekoppeld aan een bestaande stockroom met de rollen `owner`, `admin` of `member`.

## Coolify

Gebruik de Dockerfile en containerpoort `8000`.

### Verplicht

- `DATABASE_URL`: PostgreSQL connection string, bijvoorbeeld `postgresql://user:password@host:5432/stockroom`.

### E-mailverificatie en wachtwoord resetten

Voor nieuwe registraties en wachtwoord-resetlinks configureer je SMTP:

- `APP_BASE_URL`: publieke HTTPS-URL van Stockroom, bijvoorbeeld `https://stock.example.nl`.
- `SMTP_HOST`: SMTP-server.
- `SMTP_PORT`: meestal `587` voor STARTTLS of `465` voor implicit TLS.
- `SMTP_USERNAME`: SMTP-gebruikersnaam.
- `SMTP_PASSWORD`: SMTP-wachtwoord; als secret opslaan.
- `SMTP_FROM`: afzenderadres, bijvoorbeeld `Stockroom <noreply@example.nl>`.

Nieuwe registraties krijgen een verificatielink die 24 uur geldig is. Wachtwoord-resetlinks zijn 30 minuten geldig. Tokens worden alleen gehasht opgeslagen in PostgreSQL. Na een succesvolle wachtwoordreset worden alle bestaande sessies van dat account beëindigd.

Bestaande accounts van vóór de introductie van e-mailverificatie blijven geverifieerd zodat zij niet worden buitengesloten.

## Datamodel

De applicatie maakt bij het starten automatisch de benodigde tabellen aan:

- `users`
- `stockrooms`
- `memberships`
- `sessions`
- `auth_tokens`

Voorraad- en transactiedata is altijd gekoppeld aan één stockroom. De actieve gebruiker kan alleen data lezen of wijzigen van een stockroom waarvoor een membership bestaat.

## Lokaal draaien

```bash
docker build -t stockroom .
docker run --rm -p 8080:8000 \
  -e DATABASE_URL='postgresql://user:password@host:5432/stockroom' \
  -e APP_BASE_URL='http://localhost:8080' \
  -e SMTP_HOST='smtp.example.com' \
  -e SMTP_PORT='587' \
  -e SMTP_USERNAME='noreply@example.com' \
  -e SMTP_PASSWORD='secret' \
  -e SMTP_FROM='Stockroom <noreply@example.com>' \
  stockroom
```

Open daarna `http://localhost:8080`.
