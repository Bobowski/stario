"""Host header normalization used by the Cython request view."""


def host_without_port(host_str: str) -> str:
    """Lowercased Host value without a numeric port; IPv6 literals keep brackets."""
    host_str = host_str.strip()
    if not host_str:
        return ""
    if host_str.startswith("["):
        bracket_end = host_str.find("]")
        if bracket_end == -1:
            return host_str.lower()
        host = host_str[: bracket_end + 1].lower()
        rest = host_str[bracket_end + 1 :]
        if rest and (not rest.startswith(":") or not rest[1:].isdigit()):
            return host_str.lower()
        return host
    if ":" in host_str:
        host_part, _, port_part = host_str.rpartition(":")
        return (
            host_part.lower()
            if port_part.isdigit() and host_part
            else host_str.lower()
        )
    return host_str.lower()
