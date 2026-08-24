# Stockroom Android

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

## Android-beveiliging

- Alleen HTTPS als Stockroom-basis-URL
- Cleartext HTTP is uitgeschakeld in het manifest
- Mixed content wordt geblokkeerd
- File/content access in WebView is uitgeschakeld
- SSL-certificaatfouten worden nooit genegeerd
- Alleen links naar het Stockroom-host blijven in de app; externe links openen in de browser
- Cookies worden door Android WebView beheerd; de server blijft verantwoordelijk voor sessieverval en autorisatie
- Safe Browsing is ingeschakeld

## Build

- compileSdk 37
- targetSdk 36
- minSdk 26
- AGP 9.3.0 / Gradle 9.5.0 / JDK 17

De basis-URL komt uit `STOCKROOM_BASE_URL` en valt terug op:

```text
https://stock.valerith.nl
```

## Regressie vóór APK-build

De APK-workflow wordt **niet automatisch** uitgevoerd bij een push of pull request. Hij kan alleen handmatig worden gestart.

Voordat Gradle ook maar wordt ingericht of `assembleDebug` start, moet de `preflight`-job slagen. Die controleert:

- Python-syntax van de backend
- de complete rollenmatrix
- aanwezigheid van alle belangrijke webfuncties en beheer-API's
- JavaScript-syntax
- de optionele voorraadcorrectievelden
- Android WebView-beveiligingsinstellingen
- dat automatische APK-builds uitgeschakeld blijven

Workflow: `.github/workflows/android-apk.yml`

Na een geslaagde preflight bouwt de workflow:

```text
android/app/build/outputs/apk/debug/app-debug.apk
```

en publiceert artifact `stockroom-android-debug` plus release `android-latest`.
