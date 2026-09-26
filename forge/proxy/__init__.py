from forge.proxy.base import Proxy
from forge.proxy.fake import FakeProxy
from forge.proxy.traefik import ProxyError, TraefikProxy

__all__ = [
    "Proxy",
    "TraefikProxy",
    "ProxyError",
    "FakeProxy",
]
