# Security policy

## Reporting a vulnerability

If you believe you have found a security issue in Claude Developer Hub,
please report it privately rather than opening a public GitHub issue.

- Open a GitHub security advisory via the repo's Security tab
  ("Report a vulnerability"), **or**
- Email the maintainer listed on the GitHub profile.

We aim to acknowledge reports within a few days. Once an issue is
confirmed and a fix is ready, we will coordinate disclosure with you.

## What CDH does and does not do

CDH is a localhost-only tool. The backend binds to `127.0.0.1`. There is
no auth layer because the trust boundary is the local user account.

**No telemetry, no analytics, no phone-home.** The only network calls
CDH makes on your behalf are:

- `gh` (your GitHub auth, your traffic)
- `jira-cli` / `acli` if you have configured Jira (your Jira auth, your
  traffic)
- An optional local token-usage monitor on `localhost:47821` if you run
  one

CDH never transmits configuration, session data, source code, or any
file contents to an external service.

## Scope

In-scope for security reports:

- Anything that would let a process running as a different local user
  read CDH state or send commands to your iTerm2 / Claude sessions
- Anything that would let a remote attacker reach the backend despite
  the `127.0.0.1` bind
- Mishandling of secrets in config or sidecar files

Out of scope:

- Physical access to your machine
- Compromise of your `gh`, Jira, or Claude credentials by other means
