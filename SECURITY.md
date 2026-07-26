# Security policy

## Supported channel

Only the latest commit on the default branch and explicitly tagged stable
releases receive security fixes. Automatic `build-<run>-<commit>` artifacts
are development snapshots, not semantic product releases.

## Reporting

Please report vulnerabilities privately through GitHub Security Advisories for
this repository. Do not open a public issue containing credentials, exploit
details, private repository content or diagnostic bundles.

Include the affected commit/build ID, deployment profile, reproduction steps
and whether the instance was loopback-only or reachable over a network. Never
include live provider keys, authorization codes, MCP tokens or databases.

## Deployment boundary

The supported model is one user, one process, local storage. Internet-public
and multiuser deployments are unsupported. LAN deployments require the
`trusted-lan` profile, strong authentication and TLS at a reverse proxy; see
`docs/adr/ADR-001-deployment-model.md`.
