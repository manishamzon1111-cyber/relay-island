# RELAY Island — local browser proof of concept

**Status: experimental Mac-local test, not a tablet-ready or isolation-certified build.**

Uses the upstream [n.eko](https://github.com/m1k1o/neko) Firefox WebRTC image. This shows actual websites, rather than the text-only article extraction used in RELAY Island. It can also transmit supported audio and video. Playback and performance have **not** been confirmed on your M1.

## Start on M1 Mac

Requires running **Docker Desktop** (Apple Silicon). From your RELAY Island clone:

```bash
python3 browser-lab/browser_lab.py check
python3 browser-lab/browser_lab.py start
```

Open `http://127.0.0.1:18080` **on the Mac**. Login passwords are randomly generated and stored in `browser-lab/.env`, with owner-only permissions (not committed to Git). Open that file locally to view the user password. Do not paste it into chat.

To stop the separate browser container:

```bash
python3 browser-lab/browser_lab.py stop
```

The existing RELAY Island server is unchanged. This is a separate proof of concept, not a replacement.

## What you can evaluate

1. Normal Renault home page layout and links.
2. Google Search navigation, fonts, and scrolling.
3. YouTube video **and sound** in Firefox. Some codecs or DRM may not work.
4. CPU/RAM use on the M1 Mac; software encoding may be slow.

If Firefox inside the container does not render these, stop here. Do **not** open Docker ports to the tablet as a workaround.

## Explicit security boundaries

- This Compose file binds HTTP and WebRTC UDP **only to 127.0.0.1**. The tablet cannot connect directly. If Docker Desktop unexpectedly exposes them on a LAN interface, treat it as a deployment failure.
- No Mac folder, credentials, home directory, host docker socket, or media directory is mounted into the container.
- Two random passwords are generated; default n.eko passwords are not used.
- Clipboard and webcam/microphone sharing are disabled through configured member profiles.
- Browser traffic inside the container still has internet access. Never sign into sensitive personal accounts during this experiment.
- Docker Desktop and the Firefox image are additional trusted code. This does not guarantee container isolation or protection against malicious sites.
- A full browser controlled by a managed tablet can be used as an **outbound message channel** by tablet software. Blocking the tablet's direct WAN does not remove this risk. Consequently there is **no LAN/browser-share command in this release**. The earlier RELAY Island article/approval UI remains the only tablet-side interface, subject to its own limitations.
- There is no claim of protection against Halo, no claim of tested packet isolation, and no internet sharing configuration.
- WebRTC configured without public STUN/TURN and with NAT1TO1 127.0.0.1. These are local-demo settings, not instructions for opening a WAN.
- No third-party telemetry added to RELAY Island. Upstream browser/Docker behavior remains outside our control.

## Next security gate (not shipped)

Before a tablet-accessible browser is released: prove the offline LAN actually has no gateway, bound every port to the dedicated LAN interface, validate Mac forwarding is disabled, prevent other Mac services exposure, review browser access-control against an untrusted tablet client, and capture traffic during a simulated malicious requester. An interactive unrestricted remote browser cannot provide a zero-exfiltration guarantee.

Upstream configuration references: https://neko.m1k1o.net/docs/v3/installation/docker-images , https://neko.m1k1o.net/docs/v3/configuration/webrtc , https://neko.m1k1o.net/docs/v3/configuration/authentication .
