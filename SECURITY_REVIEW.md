# Security review — RELAY Island v0.5

## Trust boundary

The Android tablet is untrusted. It may initiate local connections to the Mac regardless of which Android app appears to own the browser. The Mac can have internet access, and must never act as a general-purpose NAT gateway. Admin controls must remain loopback-only.

## Implemented

- The service uses a dedicated `127.0.0.1` listener for Mac administration and, optionally, a separate tablet-facing listener.
- Admin access needs a per-run random unlock key, same-origin session cookie, custom write header and Origin / Fetch Metadata validation.
- Client code cannot submit arbitrary URLs for article retrieval. A selected search result or link from a previously approved article must supply a server-known identifier.
- All article HTTP content is parsed to a constrained JSON block model (no remote HTML/JS shipped to tablet).
- HTTPS fetcher validates URLs and globally routable resolved addresses, pins connections, revalidates redirects, and caps sizes and request times.
- Client input is inserted into the DOM via `textContent` or DOM constructors, not innerHTML.
- Static resource paths are allowlisted; media file access uses directory handles and no-follow protections.
- Local app uses system fonts and packaged assets; no CDN calls.
- Client requests are rate limited, queued and require explicit Mac approval by default, including Wikipedia article links and searches.
- Read-only macOS network preflight refuses known kernel forwarding modes. It is not a firewall.

## Not guaranteed / not tested

- A real LAVA tablet, Mac M1, and isolated Wi-Fi access point have not been tested together.
- A device can potentially encode information in allowed search terms, selected results, request frequency and packet timing. Approval reduces but cannot remove every covert channel.
- Other Mac services might be reachable from the isolated LAN without additional firewall settings.
- The app cannot distinguish Halo's local connections from browser connections if both can reach the service.
- Third-party search providers and live article extraction can change or block automated requests.
- Video codec support, Chrome-on-Android playback and YouTube streaming are not validated; there is no unrestricted YouTube proxy.
- Browser-based visual test could not run in the current sandbox: Chromium returns `ERR_BLOCKED_BY_ADMINISTRATOR` even with mocked responses.
- The 5 integration tests do not constitute a penetration test or guarantee complete safety.

## Findings addressed in v0.5

1. v0.4 had an unstyled and confusing form workflow. New client polls request status automatically and shows search results/reading states.
2. v0.4 source provided by user was missing the HTML front-end integration; reconstructed the server-facing interface and a separate Mac approval console.
3. The Claude draft's `Store._resolve_article_url` accepted arbitrary tablet-supplied URLs; removed the direct URL path to narrow indirect exfiltration.
4. Turned off default automatic approval for Wikipedia article links to require explicit Mac approval by default.
5. Found and repaired JavaScript syntax errors through Node parser checks, then added HTTP API integration tests.
