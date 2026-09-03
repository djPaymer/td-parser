def page_url(url: str, param: str, page: int) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{param}={page}"
