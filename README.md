# Audible to Exist

Sync Audible listening data into [Exist](https://exist.io/) with Python.

## What this does

This script:

- logs into Audible and stores reusable device credentials
- creates a small set of custom Exist attributes
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

2. Make a new Developer API Client on Exist.io, you will use the access key for the 'EXIST_TOKEN" environment variable.

3. Copy `.env.example` values into your shell environment.

Required variables:

- `EXIST_TOKEN`
- `AUDIBLE_LOCALE`

Optional variables:

- `AUDIBLE_USERNAME`
- `AUDIBLE_PASSWORD`
- `AUDIBLE_WITH_USERNAME`
- `AUDIBLE_AUTH_FILE_PASSWORD`
- `EXIST_USER_TIMEZONE`

## First run

Authorize Audible and save credentials:

```bash
python main.py auth --external-browser
```

Or, if you prefer direct login and have set `AUDIBLE_USERNAME` and `AUDIBLE_PASSWORD`:

```bash
python main.py auth
```

Sync today's data:

```bash
python main.py sync
```