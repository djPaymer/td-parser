from urllib.parse import urlparse


def host_key(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def same_host(left: str, right: str) -> bool:
    a, b = host_key(left), host_key(right)
    return bool(a and b and a == b)


def origin(url: str) -> str:
    text = url.strip()
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        raise ValueError(f"Bad URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def abs_url(base: str, path: str) -> str:
    path = (path or "").strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = origin(base)
    if not path.startswith("/"):
        path = "/" + path
    return base + path
