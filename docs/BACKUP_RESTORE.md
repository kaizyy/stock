# PostgreSQL backup en restore op Coolify/Hetzner

## Productie-inrichting

Koppel een persistent volume aan `/data/backups`. Bewaar daarnaast een tweede, versleutelde kopie buiten de Hetzner-server (bijvoorbeeld een S3-compatibele bucket met lifecycle-retentie). Een backup op dezelfde server is geen volwaardige calamiteitenbackup.

Configureer in Coolify een dagelijkse scheduled task, bij voorkeur buiten piekuren:

```sh
/usr/local/bin/backup-postgres.sh
```

Benodigde variabelen/secrets:

- `DATABASE_URL` (secret): productie-PostgreSQL-URL.
- `BACKUP_DIR=/data/backups`.
- `BACKUP_RETENTION_DAYS=14` (of het afgesproken bewaarbeleid).
- `RESTORE_DATABASE_URL` (alleen voor verificatie): URL van een afzonderlijke, wegwerpbare database; nooit de productie-URL.

De backup gebruikt PostgreSQL custom format, `umask 077`, een tijdelijk bestand plus atomische rename, een SHA-256 checksum en een leesbaarheidstest. Oude backups worden pas na een geslaagde nieuwe dump volgens de retentie verwijderd.

## Maandelijkse restore-verificatie

Maak een lege, tijdelijke database en voer in de applicatiecontainer uit:

```sh
RESTORE_DATABASE_URL='postgresql://…/stockroom_restore_check' /usr/local/bin/verify-restore.sh /data/backups/stockroom-YYYYMMDDTHHMMSSZ.dump
```

De verificatie controleert de checksum, herstelt binnen één transactie en controleert kritieke tabellen en leesbaarheid. Verwijder daarna de tijdelijke database. Noteer datum, backupnaam en resultaat in het operationele logboek.

## Calamiteitenrestore

1. Zet de website in maintenance mode en maak eerst een laatste snapshot als de database nog bereikbaar is.
2. Maak een nieuwe lege PostgreSQL-database met dezelfde of een nieuwere majorversie van PostgreSQL.
3. Stel `RESTORE_DATABASE_URL` in op die nieuwe database en draai `restore-postgres.sh`.
4. Draai `verify-restore.sh` tegen een extra verificatiedatabase of controleer minimaal de kritieke tabellen en aantallen.
5. Wijzig pas daarna `DATABASE_URL` in Coolify en start de applicatie opnieuw.
6. Controleer login, tenantselectie, voorraad, transacties, users, invitations en auditlog voordat maintenance mode uitgaat.

Gebruik nooit `RESTORE_DATABASE_URL=$DATABASE_URL`: restore gebruikt bewust `--clean` en vervangt de inhoud van het doel.
