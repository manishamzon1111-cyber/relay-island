# RELAY Island 0.5.4

A lightweight browser-based, Mac-hosted local reader and media player. Designed for an **offline LAN** where an untrusted Android tablet never has a public internet route. It does not provide a firewall, enforce routing, or remove device management.

## Run on your Mac

```sh
cd /path/to/relay-island-v0.5
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m relay_web --media ./media
```

Open the local interface and the **one-time approvals unlock URL printed in Terminal**. The normal `/admin` path does not create an admin session by itself. Add media files to `media/` and refresh the library.

Local testing without internet (synthetic demo results): `python3 -m relay_web --demo`. This is only for development, and the fake results must not be confused with actual search results.

The tablet-facing LAN listener only starts with both `--lan-host` and `--interface`, after read-only macOS preflight. Example, **only after building an isolated physical LAN**: `python3 -m relay_web --lan-host 192.168.50.2 --interface en7`.

### What works

- Search submitted in tablet browser; Mac approval is mandatory by default. Result page polls automatically and displays real results (Wikipedia; experimental DuckDuckGo).
- Selecting a result requests a Mac-side article extraction; supported content rendered locally as safe text/blocks, not third-party HTML.
- Local media playback with Range requests and limited subtitle support.
- Persisted request history, cancellation and rejection.
- Admin unlock via one-time local token, separate loopback listener, cookie + same-origin + custom-header validation.

### Limitations

- Google/YouTube search is **not** a full YouTube player. No arbitrary internet video proxy. MKV and some codecs may not play in Chrome.
- Network preflight does not enforce a firewall. Physical network isolation and independent packet capture are required to confirm no public route.
- The admin UI cannot be accessed from the tablet LAN listener.
- Developer demo mode returns a synthetic result and does not test the public internet.
- Android phone as a server is not implemented.

### Files

`relay_web/server.py` routes; `store.py` job state/worker; `fetcher.py` HTTPS IP-pinned fetching; `providers.py` search providers; `articles.py` safe content conversion; `media.py` video serving; `isolation.py` read-only checks; `web/` light UI.

## Search engines (important)

Default **Web search** is a best-effort DuckDuckGo HTML integration, not Google Search. It can fail if DuckDuckGo blocks automation. For more reliable web search, set `RELAY_BRAVE_API_KEY` in the Mac terminal before starting the server; this uses the official Brave Search API instead (requires separate Brave API registration and may require payment-card verification). The API key stays on the Mac, never sent to the tablet. This is **not Google results**.

Google's older Custom Search JSON API closed to new customers in January 2026. There is no hidden Google integration in this build.

If a search fails, verify that the Mac can reach the selected provider over HTTPS and that the Mac's DNS and any existing proxy/VPN permit the connection. A failure does not imply a problem with the LAVA: the Mac performs the external request.

## Alternative search sources (v0.5.2)

The **Web search** option uses DuckDuckGo HTML (unofficial) by default, or Brave's official API if `RELAY_BRAVE_API_KEY` is set. **Bing (RSS, beta)** is included as an additional no-key option; Microsoft's RSS web-search output is unofficial and may fail. Wikipedia remains a separate source, not the default.

To add an independently operated SearXNG instance with JSON enabled:

```sh
export RELAY_SEARXNG_URL=https://your-searxng-host.example
./start.command
```

SearXNG public instances frequently disable JSON or rate-limit requests; using a self-hosted, reachable HTTPS instance is more reliable. The tablet never supplies the instance URL. No automatic silent fallback sends your search query to a second provider; choose another source explicitly. Search queries are sent to whichever provider you select **after Mac approval**.

A `failed` request with `network` or `dns` indicates the Mac could not reach the external provider. Switching providers cannot fix a Mac-wide firewall, proxy, DNS, or internet connectivity problem. This build has not been verified against live search engines on your Mac.

## Full website / YouTube browser experiment (Mac only)

The old search results intentionally open in a *text-only article reader*, not a normal website. A new **n.eko / Firefox WebRTC** experiment is in [browser-lab/README.md](browser-lab/README.md). It can render normal websites in a real browser and stream audio/video; Chrome/Android compatibility and YouTube playback have **not been proven** on the target device.

**Do not open this n.eko container to the managed tablet or your LAN.** It listens on Mac loopback only; allowing an untrusted device to control a full internet browser could create an outbound data channel, even if the tablet has no internet gateway. This stage deliberately does **not** change the existing tablet app. No claim of isolation certification.

With Docker Desktop already running on an Apple Silicon Mac:

```sh
python3 browser-lab/browser_lab.py check
python3 browser-lab/browser_lab.py start
# Open http://127.0.0.1:18080 on the Mac
python3 browser-lab/browser_lab.py stop
```

Generated login passwords stay in `browser-lab/.env` (ignored by Git, owner-readable only). The launcher does not expose them in Terminal.

