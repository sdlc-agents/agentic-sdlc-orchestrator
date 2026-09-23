# URL Shortener (legacy)

The service as it exists today: create short links, resolve them through a
cache-aside read path, expire them, and deactivate them.

## What it does not do

Redirects are served and then forgotten. Nothing records who followed a link,
when, or from where, so there is no way to answer "how is this campaign doing?"
Adding that is the change the platform is asked to make against this tree.

## Layout

    app/config.py      runtime settings, all environment-overridable
    app/models.py      domain entities
    app/db.py          sqlite3 wrapper with per-thread connections
    app/shortcode.py   base62 code generation and validation
    app/cache.py       LRU + TTL cache in front of the read path
    app/repository.py  persistence for short URLs
    app/schemas.py     request/response transport models
    app/api/routes.py  HTTP surface
    app/main.py        composition root

## Running

    pip install -r requirements.txt
    uvicorn app.main:app --reload
    pytest -q
