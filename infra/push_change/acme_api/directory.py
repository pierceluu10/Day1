"""Distinguished names for Acme's LDAP employee directory."""

from __future__ import annotations

import ldap.dn

BASE_DN = "ou=people,dc=acme,dc=com"


def employee_dn(name: str, team: str) -> str:
    return ldap.dn.dn2str([[("cn", name, 1)], [("ou", team, 1)]]) + "," + BASE_DN
