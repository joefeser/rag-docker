# Security Policy

## Reporting a vulnerability

Please **do not** report security vulnerabilities in public issues, pull
requests, or discussions.

Report them privately through GitHub instead:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Describe the issue, how to reproduce it, and the impact you expect.

You'll get a reply in the private advisory. Once a fix is ready, we'll publish
the advisory and credit you unless you'd rather stay anonymous.

## Supported versions

Releases are tagged with semantic versions (`v1.0.0` onward). Only the latest
release gets security fixes: currently **1.0.x**. A fix lands on `develop`
first and ships in the next release.

## Scope

This platform is built to run locally, and it has no authentication.

- **Loopback by default.** The proxy is published on host loopback,
  `127.0.0.1:8080`, so other machines can't reach it. That holds on Docker
  Engine 28.0.0 or newer: Docker documents that older engines still let hosts
  on the same network reach localhost-published ports. Engine 28.0.0 or newer
  is a prerequisite.
- **Web pages in a local browser can still reach it.** The API accepts
  requests from any origin (CORS), so a web page open in a browser on the same
  machine can call it. This is tracked in #25.

Exposing the workbench to an untrusted network is outside the supported
configuration, but reports of issues that make that riskier than expected are
still welcome.
