# User-Agent: observations and compatibility plan

## Scope

This note records the HTTP User-Agent observations gathered during diagnostics on 12 October 2026 and the planned adapter change. It does not claim that a 403 Forbidden response always means the same thing or that changing a header guarantees access.

## What the adapter sends today

In adapter **v2.2.10**, jacred_v2_adapter.py sends the following project-specific headers to the JacRed JSON API:

    User-Agent: JacRed-V2-Torznab-Adapter/2.2.10
    Accept: application/json

The version component is produced from the running adapter version.

## Observations from 2026-10-12

### sync.jacred.stream

Requests to https://sync.jacred.stream/sync/conf without an explicit browser-style User-Agent returned a Cloudflare HTTP 403 page during the investigation. Requests with an explicit Windows/Chrome User-Agent returned HTTP 200 and JSON similar to:

    {"fbd":true,"spidr":true,"version":2}

The difference was also observed when the request was pinned to a specific Cloudflare IP. Both the Chrome 155 test header and the Chrome 111 header described below received HTTP 200 in the tested calls.

This is evidence that request headers — in particular User-Agent — were relevant to the outcome for this endpoint and those tests. It is not proof that User-Agent was the only variable involved or that every 403 from any JacRed endpoint has the same cause.

### Public JacRed API

A request to https://jac.red/torznab/api?t=caps with a Chrome 155 User-Agent returned HTTP 200 during testing. Separate requests to https://jac.red/ and a v2 JSON endpoint had returned HTTP 403 at another point in the investigation. These are different endpoints and observations; they must not be collapsed into a claim that one header universally fixes access.

### What the upstream JacRed client uses

In the upstream source inspected on 12 October 2026, [Infrastructure/Networking/HttpClient.cs](https://github.com/jacred-fdb/jacred/blob/main/Infrastructure/Networking/HttpClient.cs) set this User-Agent for its HTTP client:

    Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36

This is a useful compatibility reference because it is the value used by JacRed's own client in the inspected source. It should be rechecked against the upstream version at implementation time; it is not a permanent API contract.

## Decision for the next adapter version

Replace the adapter-specific User-Agent default (JacRed-V2-Torznab-Adapter/{VERSION}) with a stable value aligned with the one used by the upstream JacRed HTTP client, after verifying the then-current upstream source. If the value is made configurable, document JACRED_USER_AGENT and give it that fixed compatibility default.

Do not randomly rotate User-Agent values or change them in reaction to individual errors. Prefer a stable, traceable value and keep the same value across upstream requests.

## Important restriction for diagnostics

During the incident, public JacRed returned HTTP 403 responses without an explicit expiry time. The existing roadmap therefore requires a protection/cooldown period and one controlled recovery check. The planned User-Agent change must **not** be used to probe repeatedly, circumvent an active restriction, or infer that the source has lifted a block.

Before the protection period expires:

- test request construction, User-Agent consistency, HTTP 403/429 handling and circuit-breaker logic against a local mock HTTP server;
- do not issue repeated live requests to public JacRed to experiment with headers.

After the protection period, perform only the single controlled live recovery attempt already specified in [ROADMAP.md](../ROADMAP.md). If the request still returns 403, preserve the protection state and follow the documented recovery policy.

## Implementation checks

- [ ] Confirm the intended default against the upstream source version being targeted.
- [ ] Replace the current adapter-specific default in the next release.
- [ ] If supported, document JACRED_USER_AGENT and its default in both READMEs and deployment instructions.
- [ ] Add a local-mock test asserting that the same configured header is sent consistently.
- [ ] Ensure a 403 still activates the protection policy regardless of the User-Agent.
- [ ] Do not mark upstream connectivity as restored based solely on changing the header.
