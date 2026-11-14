"""
A qué direcciones se ata el puente.

## El problema que esto resuelve

El cuerpo corre nativo en Windows y el cerebro en WSL. Parece obvio que se
hablen por `127.0.0.1`, pero **no funciona**: con `networkingMode=NAT` (que es
lo que hay configurado en `.wslconfig`) la VM de WSL tiene su propia pila de red
y su propio loopback. Verificado en esta máquina: con el cuerpo escuchando en
`127.0.0.1:8765` del lado Windows, desde WSL **ninguna** dirección lo alcanza —
ni el loopback, ni el gateway, ni el hostname.

El reenvío de localhost de WSL es de una sola dirección: Windows puede alcanzar
servicios de WSL por `localhost`, pero no al revés.

## La solución

Las dos puntas están conectadas por un adaptador virtual privado. Acá se llama
`vEthernet (WSL (Hyper-V firewall))` y tiene `172.27.128.1/20`; desde WSL esa
misma IP es el default gateway. Entonces el puente se ata a **loopback más esa
dirección**, y el cerebro se conecta a su propio gateway.

Lo importante es lo que NO se hace: atarse a `0.0.0.0`. Esta máquina tiene
Wi-Fi, así que `0.0.0.0` publicaría el puente en la red del
café. El filtro de abajo se queda solo con las privadas de `172.16/12`, que es
el rango que usa el adaptador de WSL, y descarta explícitamente la LAN.

(El CVE-2026-25253 de OpenClaw — RCE de un click, CVSS 8.8 — fue exactamente
este descuido, y eso estando en loopback. Con un puerto en la Wi-Fi hubiera sido
peor.)
"""

from __future__ import annotations

import ipaddress
import socket

from loguru import logger

LOOPBACK = "127.0.0.1"

# El adaptador de WSL vive en 172.16.0.0/12. La LAN doméstica típica está en
# 192.168.0.0/16 y la corporativa en 10.0.0.0/8: las dos quedan afuera a
# propósito.
WSL_RANGE = ipaddress.ip_network("172.16.0.0/12")


def local_ipv4_addresses() -> list[str]:
    """Todas las IPv4 de esta máquina, sin dependencias externas."""
    found: set[str] = set()
    host = socket.gethostname()

    try:
        _, _, addresses = socket.gethostbyname_ex(host)
        found.update(addresses)
    except OSError as exc:
        logger.debug(f"gethostbyname_ex falló: {exc}")

    try:
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            found.add(info[4][0])
    except OSError as exc:
        logger.debug(f"getaddrinfo falló: {exc}")

    return sorted(found)


def wsl_facing_addresses() -> list[str]:
    """
    Las direcciones por las que la VM de WSL puede alcanzar a este host.

    Filtra por el rango del adaptador virtual. Si devuelve vacío, o no hay WSL
    instalado, o el adaptador está abajo — en cualquiera de los dos casos el
    puente se queda en loopback y lo dice.
    """
    matches = []
    for address in local_ipv4_addresses():
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed in WSL_RANGE and parsed.is_private:
            matches.append(address)
    return matches


def bind_hosts(*, allow_wsl: bool = True) -> list[str]:
    """
    Las direcciones a las que se ata el puente.

    Siempre loopback. Más las que miran a WSL, si se pide y existen.
    """
    hosts = [LOOPBACK]

    if not allow_wsl:
        logger.info("Puente solo en loopback (acceso desde WSL deshabilitado)")
        return hosts

    extra = wsl_facing_addresses()
    if not extra:
        logger.warning(
            "No encontré el adaptador de WSL. El puente queda solo en loopback, "
            "así que un cerebro corriendo en WSL no va a poder conectarse."
        )
        return hosts

    hosts.extend(extra)
    logger.info(f"Puente accesible desde WSL en: {', '.join(extra)}")
    return hosts


def windows_host_from_wsl() -> str | None:
    """
    Desde WSL, la IP del host Windows: el default gateway.

    Se lee de `/proc/net/route`, que no depende de que `ip` esté instalado.
    Devuelve None si no se puede determinar (por ejemplo si no estamos en WSL).
    """
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            next(handle)  # encabezado
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                # Destino 00000000 = ruta default. El gateway viene en hex
                # little-endian.
                if fields[1] == "00000000":
                    raw = int(fields[2], 16)
                    octets = [(raw >> shift) & 0xFF for shift in (0, 8, 16, 24)]
                    return ".".join(str(o) for o in octets)
    except (OSError, StopIteration, ValueError) as exc:
        logger.debug(f"No pude leer el gateway de /proc/net/route: {exc}")
    return None
