# Android-functiepariteit

De Android-app toont dezelfde beveiligde Stockroom-interface en gebruikt dezelfde API, PostgreSQL-data, sessie en rollen als de website. Daardoor is onderstaande functionaliteit één gedeelde implementatie en geen los te synchroniseren Android-equivalent.

| Onderdeel | Android-route | Controle |
|---|---|---|
| Dashboard, analytics en meldingen | Gedeelde webinterface | JavaScript-regressie + API-tests |
| Voorraad, archief, correcties en reserveringen | Gedeelde webinterface | Workflow- en voorraadtests |
| Inkomende/uitgaande transacties en betaalmethode | Gedeelde webinterface | Transactie-regressietests |
| Relaties, inkoop- en verkooporders | Gedeelde webinterface | Database-integratietests |
| Offertes, omzetting en voorraadreservering | Gedeelde webinterface | Offerte-workflowtests |
| Facturen, betalingen, creditnota's en prullenbak | Gedeelde webinterface | Financiële workflowtests |
| Factuur/offerte mailen | Gedeelde API; Android-bestands/PDF-ondersteuning | Document- en workflowtests |
| Barcodes en QR-camera | Webcamera via gecontroleerde Android CAMERA-toestemming | Android-pariteitscontrole + Lint |
| Logo/importbestand kiezen | Android Storage Access Framework | Android-pariteitscontrole + Lint |
| Gebruikers, rollen, uitnodigingen en meerdere stockrooms | Gedeelde webinterface en sessie | Rollenmatrix + API-tests |
| Instellingen, beveiliging, audit, back-up en platformbeheer | Gedeelde webinterface, rolbeveiligd | Regressiesuite en rechtenmatrix |
| PDF's en externe links | Vertrouwde host intern; overige hosts extern | Android-pariteitscontrole |
| Offline/netwerkfout | Android-foutscherm met opnieuw proberen | Android-pariteitscontrole + Lint |

## Vrijgavepoort

Een nieuwe APK wordt alleen gepubliceerd nadat PostgreSQL-integratietests, Python-controles, alle JavaScript-tests, Android-pariteitscontroles, Android Lint en `assembleDebug` zijn geslaagd.
