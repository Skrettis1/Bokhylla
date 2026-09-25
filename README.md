# Bokhylla for Debian 13

En lokal webapp for forfattere, boklister, lest-status, A/H/T-tagger, Open Library-synkronisering, omslags-cache og ukentlig oppdatering.

## Plattform

- Debian 13 med Python 3.13 eller nyere
- Fast nettverk anbefales under synkronisering
- Minst 2 GB ledig lagringsplass anbefales

Appen kjører direkte i et virtuelt Python-miljø og installeres som en systemd-tjeneste. Den kan kjøres på både x86_64 og ARM64-maskiner med Debian 13.

## Installer

1. Pakk ut prosjektet på Debian-maskinen.
2. Endre `OPENLIBRARY_CONTACT` i `.env.example` til din e-postadresse, eller rediger `.env` etter første kjøring.
3. Kjør:

```bash
cd bokhylla-rock3c
./install.sh
```

Åpne `http://ENHETENS-IP:8080` (eller porten du setter i `APP_PORT`).

## Nyttige kommandoer

```bash
sudo systemctl status bokhylla
sudo journalctl -u bokhylla -f
sudo systemctl restart bokhylla
sudo systemctl stop bokhylla
```

## Data og sikkerhetskopi

Vedvarende data ligger i `./data`:

- `library.db`: SQLite-database
- `covers/`: lokalt mellomlagrede bokomslag
- `backups/`: de ti siste databasesikkerhetskopiene

Ta gjerne en ekstra sikkerhetskopi av hele `data`-mappen.

## Synkronisering

- Manuell synkronisering per forfatter i GUI-et
- Manuell synkronisering av alle forfattere
- Ukentlig synkronisering, standard søndag kl. 03:00
- Endres i `.env` med `SYNC_DAY` og `SYNC_HOUR`

## Viktig om serier

Open Library har ikke alltid komplette serieopplysninger. Nye verk plasseres derfor under `Ikke plassert i serie`. API-et støtter feltene `series_name` og `series_order`, men redigeringsskjerm for disse er neste funksjonelle steg.

## Første oppstart

Forfatterne opprettes automatisk, men boklistene er tomme. Velg en forfatter og trykk `Hent fra Open Library`.
