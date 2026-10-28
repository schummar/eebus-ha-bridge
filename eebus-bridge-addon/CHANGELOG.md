# Changelog

Add-on versions track the bridge release they package, so this file records
only what changed in the packaging. Bridge changes are in the
[project releases](https://github.com/volschin/eebus-ha-bridge/releases).

## Unreleased

- New option `ohpcf_enabled` (default `true`): set to `false` when another
  energy manager (e.g. evcc) should control the heat pump's compressor
  flexibility (OHPCF).
- New option `ship_log` (default `false`): logs SHIP/SPINE connection details
  to diagnose a heat pump that does not connect.
- New option `experimental_mgcp_provider` (default off) exports
  `EEBUS_EXP_MGCP_PROVIDER` so the experimental MGCP grid provider can be
  enabled from the add-on configuration.

## 0.16.2

- Initial release of the add-on.
