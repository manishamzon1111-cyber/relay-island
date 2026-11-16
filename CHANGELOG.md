## 0.5.4 — Fix live search HTTP response crash

- Fixed `OSError: [Errno 9] Bad file descriptor` after a completed Content-Length HTTP response.
- A completed Python HTTPResponse can close the socket; the fetch loop now checks for completion before touching it again.
- Added live local HTTP-server regression tests for Content-Length and connection-close responses.
- Verified locally: 13 unittest tests pass, including direct reproduction of the original failure before the fix.
- Live Bing retrieval on the user's Mac remains unverified until the updated server is run there.

## 0.5.3 — Search-first UI and connection diagnostics

- Replaced the cramped search panel with a responsive three-column search form (tablet layout stacks provider picker below).
- Changed the initial provider selection from DuckDuckGo to Bing RSS for general web search; Wikipedia is optional.
- Added provider notes directly below search and a retry action after a failed request.
- Display Mac-side provider hostname on failed searches; log TCP/TLS/HTTP errors to the Mac Terminal for troubleshooting.
- Corrected the reported application version.
- Verification: 11 existing automated tests pass and JavaScript syntax validation passes in the development container. This does **not** establish live provider reliability or a successful browser screenshot test.
- Security: This is **not** a firewall or network-isolation proof. Do not expose the managed tablet to an internet-connected network.

# v0.5.0

- Reconstructed functional Mac backend from supplied partial Claude source.
- Added completed lightweight tablet/client frontend and separate admin approval UI (light theme).
- Added submit → pending → approved → results polling and result → article request flow.
- Added local media library and HTML5 playback integration.
- Added session-based Mac admin unlock, local-only listener separation, and restricted API control flows.
- Removed arbitrary article URL submission from untrusted tablet and disabled automatic article approval by default.
- Added CLI, one-command Mac local start, security notes and five HTTP integration tests.

**Not done:** actual isolated-network configuration, on-device tablet test, real browser screenshot testing, full YouTube functionality, phone-hosted server, live internet provider validation.

## 0.5.2 — provider options
- Added experimental keyless Bing RSS search as a selectable alternative.
- Added optional SearXNG JSON provider via Mac-side `RELAY_SEARXNG_URL`.
- Retained official Brave API support and DuckDuckGo HTML as default without API key.
- Improved empty-result/error messages; no silent forwarding of queries to additional providers.
- Added six provider regression tests; 11 total automated tests pass.
- Live search availability on the user's Mac remains unverified.
