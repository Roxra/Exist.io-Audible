# Audible to Exist

Sync Audible listening data into [Exist](https://exist.io/) with Python. Uses the [Audible Python API](https://github.com/mkb79/Audible).

## What this does

This script:

- authorizes with Audible in your browser and stores reusable device credentials locally
- fetches today's Audible listening stats
- writes the values into Exist

## Attributes created in Exist

- `Audible listening` as minutes (Media)
- `Audible books finished` as an integer count (Media)

## Setup

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Make a new Developer API Client on Exist.io (Account Settings -> Developer Clients -> Add a new client)
- Name = Whatever you want!
- Support Email = Whatever you want!
- Redirect URI = http://localhost:8000/
- OAuth2 client type = Confidential

You will use the resulting access key for the 'EXIST_TOKEN" environment variable.

3. Copy `.env.example` values into your shell environment.

Required variables:

- `EXIST_TOKEN`
- `AUDIBLE_LOCALE`

Optional variables:

- `EXIST_USER_TIMEZONE`

## Local files

- `audible_auth.json` stores the reusable Audible device credentials created by `python main.py auth`

## First run

Authorize Audible in your browser and save credentials:

```bash
python main.py auth
```

Sync today's data:

```bash
python main.py sync
```

I'd recommend using Task Scheduler (or any equivalent) to run this script once a day at whatever time works best for your timezone and Audible's data availability.
