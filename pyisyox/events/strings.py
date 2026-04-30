"""Strings for Event Stream Requests."""


def sub_head(url: str, auth: bytes, length: int) -> str:
    """Return the Subscribe HTTP header."""
    return (
        f"POST /services HTTP/1.1\r\n"
        f"Host: {url}\r\n"
        f"Authorization: {auth}\r\n"
        f"Content-Length: {length}\r\n"
        f'Content-Type: text/xml; charset="utf-8"\r\n'
        f"SOAPAction: urn:udi-com:device:X_Insteon_Lighting_Service:1#Subscribe\r\n"
        f"\r\n"
    )


def sub_body() -> str:
    """Return the Subscribe HTTP body."""
    return (
        "<s:Envelope><s:Body>\n"
        '<u:Subscribe xmlns:u="urn:udi-com:service:X_Insteon_Lighting_Service:1">\n'
        "<reportURL>REUSE_SOCKET</reportURL>\n"
        "<duration>infinite</duration>\n"
        "</u:Subscribe></s:Body></s:Envelope>\n"
        "\r\n"
    )


def unsub_head(url: str, auth: bytes, length: int) -> str:
    """Return the Unsubscribe HTTP header."""
    return (
        f"POST /services HTTP/1.1\r\n"
        f"Host: {url}\r\n"
        f"Authorization: {auth}\r\n"
        f"Content-Length: {length}\r\n"
        f'Content-Type: text/xml; charset="utf-8"\r\n'
        f"SOAPAction: urn:udi-com:device:X_Insteon_Lighting_Service:1#Unsubscribe\r\n"
        f"\r\n"
    )


def unsub_body(sid: str) -> str:
    """Return the Unsubscribe HTTP body."""
    return (
        "<s:Envelope><s:Body>\n"
        '<u:Unsubscribe xmlns:u="urn:udi-com:service:X_Insteon_Lighting_Service:1">\n'
        f"<SID>{sid}</SID>\n"
        "</u:Unsubscribe></s:Body></s:Envelope>\n"
        "\r\n"
    )


def resub_head(url: str, auth: bytes, length: int) -> str:
    """Return the Resubscribe HTTP header."""
    return (
        f"POST /services HTTP/1.1\r\n"
        f"Host: {url}\r\n"
        f"Authorization: {auth}\r\n"
        f"Content-Length: {length}\r\n"
        f'Content-Type: text/xml; charset="utf-8"\r\n'
        f"SOAPAction: urn:udi-com:device:X_Insteon_Lighting_Service:1#Subscribe\r\n"
        f"\r\n"
    )


def resub_body(sid: str) -> str:
    """Return the Resubscribe HTTP body."""
    return (
        "<s:Envelope><s:Body>\n"
        '<u:Subscribe xmlns:u="urn:udi-com:service:X_Insteon_Lighting_Service:1">\n'
        "<reportURL>REUSE_SOCKET</reportURL>\n"
        "<duration>infinite</duration>\n"
        f"<SID>{sid}</SID>\n"
        "</u:Subscribe></s:Body></s:Envelope>\n"
        "\r\n"
    )
