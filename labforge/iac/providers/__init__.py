from labforge.iac.providers.base import Provider, ProviderError
from labforge.iac.providers.container import ContainerProvider
from labforge.iac.providers.host import HostProvider
from labforge.iac.providers.loadbalancer import LoadBalancerProvider
from labforge.iac.providers.network import NetworkProvider

PROVIDERS: dict[str, Provider] = {
    "network": NetworkProvider(),
    "host": HostProvider(),
    "loadbalancer": LoadBalancerProvider(),
    "container": ContainerProvider(),
}

__all__ = ["PROVIDERS", "Provider", "ProviderError"]
