# Audible to Exist

Sync Audible listening data into [Exist](https://exist.io/) with Python. Uses the [Audible Python API](https://github.com/mkb79/Audible).

## What this does

This script:

- authorizes with Audible in your browser and stores reusable device credentials locally
- creates a small set of custom Exist attributes and caches their internal names locally
- fetches today's Audible listening stats
- writes the values into Exist

## Attributes created in Exist

- `Audible listening` as minutes
- `Audible books finished` as an integer count

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

This includes `tzdata`, which helps Python resolve IANA timezones such as `Europe/London` on Windows.

2. Make a new Developer API Client on Exist.io, you will use the access key for the 'EXIST_TOKEN" environment variable.

3. Copy `.env.example` values into your shell environment.

Required variables:

- `EXIST_TOKEN`
- `AUDIBLE_LOCALE`

Optional variables:

- `EXIST_USER_TIMEZONE`

## Local files

- `audible_auth.json` stores the reusable Audible device credentials created by `python main.py auth`
- `exist_attributes.json` caches the Exist attribute names so future syncs do not need to recreate them

If `exist_attributes.json` is deleted, the script will rebuild it by reading your existing attributes from Exist.

## First run

Authorize Audible in your browser and save credentials:

```bash
python main.py auth
```

Sync today's data:

```bash
python main.py sync
```
