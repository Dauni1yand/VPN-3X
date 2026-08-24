"""The REALITY constants every inbound this project builds has to carry.

The payload builder that used to live here went with 3x-ui: that panel took
`settings`/`streamSettings` as JSON-encoded strings, a quirk of its Go
models, so the shape was specific to it. Remnawave takes an ordinary Xray
config, and remnawave_provisioner builds it.

What survives is the part that was never about the panel.
"""

from __future__ import annotations


REALITY_PORT = 443

# Oldest xray-core a client may run and still be accepted.
#
# This MUST be set explicitly. Left unset, xray-core does not mean "any
# version" -- infra/conf/transport_security.go falls through to
#
#     config.MinClientVer = []byte{26, 3, 27}
#     LogWarning("REALITY: The default minimal client version is Xray-core
#                 v26.3.27, other clients may be refused to connect")
#
# and refuses every client on an older core. That is nearly every real app
# (v2rayNG, Hiddify, Streisand, NekoBox...), which presents as a config that
# looks perfect and simply never connects.
#
# 1.8.0 is where REALITY + xtls-rprx-vision stabilised, so it accepts every
# client in practical use. The tradeoff xray warns about is real: a lower
# floor lets an old or forged client probe the server, which is what the
# high default is defending against. Being connectable wins here -- a VPN
# nobody's client can reach is not a hardened VPN.
MIN_CLIENT_VERSION = "1.8.0"
