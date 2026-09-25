"""Host header normalization used by the Cython request view."""


def host_without_port(host_str: str) -> str:
    """Lowercased Host value without a numeric port; IPv6 literals keep brackets.

    One trailing dot is dropped: ``example.com.`` (the fully qualified form)
    names the same host as ``example.com``.
    """
    return _strip_root_dot(_host_without_port(host_str))


def _strip_root_dot(host: str) -> str:
    if len(host) > 1 and host.endswith(".") and not host.endswith(".."):
        return host[:-1]
    return host


def _host_without_port(host_str: str) -> str:
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
            host_part.lower() if port_part.isdigit() and host_part else host_str.lower()
        )
    return host_str.lower()
