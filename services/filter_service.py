from config import AD_KEYWORDS


def ad_keyword_hit(text: str) -> str | None:
    clean = (text or "").strip().lower()
    if not clean:
        return None
    for keyword in AD_KEYWORDS:
        kw = (keyword or "").strip().lower()
        if kw and kw in clean:
            return keyword
    return None
