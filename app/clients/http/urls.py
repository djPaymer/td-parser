from urllib.parse import urldefrag, urljoin, urlparse


def host_key(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def same_host(left: str, right: str) -> bool:
    a, b = host_key(left), host_key(right)
    return bool(a and b and a == b)


def with_scheme(url: str) -> str:
    text = (url or "").strip()
    if text and not text.startswith(("http://", "https://")):
        text = "https://" + text
    return text


def origin(url: str) -> str:
    parsed = urlparse(with_scheme(url))
    if not parsed.netloc:
        raise ValueError(f"Bad URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def abs_url(base: str, href: str) -> str:
    """Resolve ``href`` against ``base`` (a page URL or a site origin); drops fragments."""

    href = (href or "").strip()
    if href.startswith(("http://", "https://")):
        return urldefrag(href)[0]
    if href.startswith("//"):
        return urldefrag(urlparse(with_scheme(base)).scheme + ":" + href)[0]
    base = with_scheme(base)
    if not urlparse(base).path:
        base += "/"
    return urldefrag(urljoin(base, href))[0]
