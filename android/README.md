# Stockroom Android 2.0

De Android-app gebruikt de **exacte Stockroom-webinterface** in een beveiligde Android WebView. Daardoor gebruikt Android dezelfde PostgreSQL-data, dezelfde sessies, dezelfde rollen en exact dezelfde functionaliteit als de website. Er is geen tweede voorraad- of rechtenimplementatie meer die kan afwijken.

## Functionaliteit

Alles wat op de website beschikbaar is, is ook in de app beschikbaar:

- Overzicht en rolbewuste KPI's
- Voorraad en artikelen
- Inkomend en uitgaand
- Betaling- en leverstatus
- Archief, herstellen en definitief verwijderen
- Meerdere stockrooms en wisselen tussen stockrooms
- Gebruikers en rollen
- Uitnodigingen per e-mail
- Categorie, leverancier en minimumvoorraad
- Voorraadcorrecties
- Lage-voorraadwaarschuwingen
- Auditlog
- Handmatig uitloggen
- Account permanent verwijderen
- Dezelfde maximale sessieduur van 30 minuten

Nieuwe webfunctionaliteit wordt automatisch ook Android-functionaliteit zolang die via dezelfde webinterface wordt aangeboden.

Daarnaast ondersteunt de Android-container nu alle mobiele randfuncties die de webinterface nodig heeft:

- Camera-toestemming voor barcode- en QR-scanning
- Veilige Android-bestandskiezer voor logo's en imports
- PDF- en nieuwe-vensterlinks binnen de vertrouwde Stockroom-host
- Externe links via een geschikte Android-app
- Herstel van sessie en navigatie na schermrotatie
- Een eigen verbindingsfoutscherm met opnieuw proberen

## Android-beveiliging

- Alleen HTTPS als Stockroom-basis-URL
- Cleartext HTTP is uitgeschakeld in het manifest
- Mixed content wordt geblokkeerd
- Rechtstreekse bestandstoegang is uitgeschakeld; alleen door de gebruiker gekozen `content://`-bestanden zijn toegestaan
- SSL-certificaatfouten worden nooit genegeerd
- Alleen links naar het Stockroom-host blijven in de app; externe links openen in de browser
- Cookies worden door Android WebView beheerd; de server blijft verantwoordelijk voor sessieverval en autorisatie
- Safe Browsing is ingeschakeld

## Build

- compileSdk 37
- targetSdk 36
- minSdk 26
- Android 2.0.0 (versionCode 3)
- AGP 9.3.0 / Gradle 9.5.0 / JDK 17

De basis-URL komt uit `STOCKROOM_BASE_URL` en valt terug op:

```text
https://stock.valerith.nl
```

## Regressie vóór APK-build

De APK-workflow wordt **niet automatisch** uitgevoerd bij een push of pull request. Hij kan alleen handmatig worden gestart.

Voordat de APK wordt gepubliceerd moet de `preflight`-job slagen. Die controleert:

- Python-syntax van de backend
- de complete rollenmatrix
- aanwezigheid van alle belangrijke webfuncties en beheer-API's
- alle database-integratietests tegen PostgreSQL 16
- JavaScript-syntax en alle JavaScript-regressietests
- de optionele voorraadcorrectievelden
- Android WebView-beveiligingsinstellingen
- dat automatische APK-builds uitgeschakeld blijven

Workflow: `.github/workflows/android-apk.yml`

Na een geslaagde preflight voert de workflow Android Lint uit en bouwt daarna:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

en publiceert het downloadbare Actions-artifact `stockroom-android-debug`. De workflow heeft alleen leesrechten op de repository en verwijdert of overschrijft geen releases.
