"""Canary tags: unique strings planted in fake secrets. If one ever leaves an
environment, we know a real secret was stolen. Everything here is fake."""

TAGS = {
    "BAIT-AWS": "AWS secret key",
    "BAIT-DB": "database password",
}


def tags_in(text):
    return [desc for tag, desc in TAGS.items() if tag in text]
