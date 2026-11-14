"""Feature record helpers shared by the six families."""

METHODS = ("exact", "derived", "assumed")


def feat(key, value, unit="", method="exact", source="", note=""):
    if method not in METHODS:
        raise ValueError("unknown method: %s" % method)
    return {
        "key": key,
        "value": value,
        "unit": unit,
        "method": method,
        "source": source,
        "note": note,
    }


def family(fid, name, question, items):
    return {"id": fid, "name": name, "question": question, "items": items}


def r2(x, n=2):
    if x is None:
        return None
    return round(float(x), n)
