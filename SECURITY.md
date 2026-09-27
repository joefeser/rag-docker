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

Only the latest commit on the default branch gets security fixes.

## Scope

This platform is built to run locally. By default the proxy publishes port
8080 on the host and has no authentication. Exposing it to an untrusted
network is outside the supported configuration, but reports of issues that
make that riskier than expected are still welcome.
