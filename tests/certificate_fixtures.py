"""Public test-only trust material; no developer files or inventory reads."""

# Minimal valid self-signed EC certificate; not trusted corporate material.
# Moved unchanged from test_constructor_corporate_network_red._VALID_PEM.
TEST_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBfDCCASOgAwIBAgIUdl75ym6g4Eko1mak72ORwWiNWnowCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI2MDgxNDAwMTgxMVoXDTI3MDgxNDAw
MTgxMVowFDESMBAGA1UEAwwJbG9jYWxob3N0MFkwEwYHKoZIzj0CAQYIKoZIzj0D
AQcDQgAELD/pSr/tM52haJmAs3Lm1YJ/m3AGrlvGgj38Fa+I/95Uo1x/zbKyFmZI
T9PWiGwS24UiLM+aFsAd+rPV0KV7eKNTMFEwHQYDVR0OBBYEFHF9WJm24ZXta8tj
gpJslBtSd+8LMB8GA1UdIwQYMBaAFHF9WJm24ZXta8tjgpJslBtSd+8LMA8GA1Ud
EwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDRwAwRAIgTYW37JoqGZtM1MCI3cahfZRT
WpbFH/15SPYF7RkD3HECIC8tEfgidvmwIGG2J1cOKTnJeHgSJdEiya9SAZ6QKD0/
-----END CERTIFICATE-----
"""
