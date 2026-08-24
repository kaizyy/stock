# Stockroom Android

Native Android-client voor dezelfde Stockroom-backend en PostgreSQL-data als het webdashboard.

## Stack

- Kotlin + Jetpack Compose
- compileSdk 37
- targetSdk 36
- minSdk 26
- AGP 9.3.0 / Gradle 9.5.0 / JDK 17
- Compose BOM 2026.08.00

## Backend

De app gebruikt dezelfde sessies en rollen als het webdashboard:

- `POST /api/mobile/login`
- `POST /api/mobile/logout`
- `POST /api/mobile/switch-stockroom`
- `GET /api/me`
- `GET /api/state`
- `PUT /api/state`

De sessie blijft maximaal 30 minuten geldig, net als op het web. Het sessietoken wordt lokaal alleen gebruikt als cookie voor de Stockroom-API.

## URL instellen

Bouw altijd tegen de publieke HTTPS-URL van jouw Stockroom-installatie:

```bash
gradle -p android assembleDebug -PSTOCKROOM_BASE_URL=https://stock.jouwdomein.nl
```

Voor GitHub Actions kun je onder **Repository settings → Secrets and variables → Actions → Variables** de repository variable `STOCKROOM_BASE_URL` instellen.

## APK

Workflow: `.github/workflows/android-apk.yml`

De workflow bouwt `android/app/build/outputs/apk/debug/app-debug.apk` en publiceert hem als artifact `stockroom-android-debug`.

## Rollen

De Android-interface gebruikt `/api/me` als bron voor rechten. De backend blijft altijd de definitieve autorisatie uitvoeren.

- Owner/Admin/Gebruiker: volledig operationeel volgens de webrechten.
- Inkoper: alleen inkomend.
- Verkoper: alleen uitgaand.
- Viewer: alleen-lezen.
