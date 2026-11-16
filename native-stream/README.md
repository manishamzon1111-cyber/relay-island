# RELAY Island — native browser window stream (no Docker)

**This is a view-only experiment, not a full browsing solution or an isolation guarantee.** It runs directly on Apple Silicon macOS with [Lumen](https://github.com/flaviodelgrosso/lumen), an independent AGPL-3.0 project. Lumen uses ScreenCaptureKit + VideoToolbox + WebRTC. No Linux VM, n.eko, Docker or remote Chromium installation.

## What it does

Stream one **existing browser window** and Mac audio to Chrome on a tablet using an independent LAN. The tablet sees real sites (Renault, Google, YouTube), instead of RELAY Island's extracted text, and can watch supported videos with sound. **The Mac controls the browser; the tablet receives video and audio.** The tablet cannot click links, type into Google or control YouTube through this mode.

**Do not use the managed tablet on a normal internet-connected household hotspot/router** for the sake of testing this. This integration requires a properly segregated local-only access point and dedicated Mac LAN adapter.

## Install lightweight host (one-time)

On M1 macOS 13+, with Homebrew:

```sh
brew install flaviodelgrosso/formulae/lumen
lumen --version
lumen windows
```

This Homebrew command is documented by Lumen upstream; the binary and its dependencies still occupy some disk space. We have not benchmarked actual disk or RAM use on your Mac. Review the open-source repository before installing.

## Before any tablet test

1. Connect the Mac to the internet **separately** from the dedicated offline LAN adapter. The LAN must have **no WAN, uplink, NAT, default router or repeater mode**; disable Mac Internet Sharing.
2. Put the managed LAVA tablet on that dedicated offline LAN, with no other internet-bearing connection. This must be verified, not assumed from a local IP address.
3. Configure a private static IPv4 on the Mac's dedicated LAN interface, e.g. `192.168.50.2/24`, and find the adapter name with `ifconfig`. Do not blindly use `en7`; its identity differs on each Mac.
4. Open an ordinary **Chrome browser window on the Mac** (not your personal desktop), then list windows via `lumen windows`. Use that browser window's real ID.
5. Run the following **only after independently verifying the network configuration**:
   
```sh
cd ~/relay-island
python3 native-stream/run.py --host 192.168.50.2 --interface en7 --window 1234 2>&1 | tee >(pbcopy)
```

Replace both `en7` and `1234` with values from your Mac. Do **not** change ports or bind to `0.0.0.0`.

Lumen prints the LAN viewer URL, generally `http://192.168.50.2:3131`; use the exact URL it prints. Open it from tablet Chrome and **approve that viewer on the Mac**. Do not enable `--auto-accept`. Click/touch on the tablet controls only the video-player UI; it does not control the Mac browser.

The runner checks the Mac's interfaces, kernel forwarding and routing before launching and every three seconds. If it sees a failure it terminates the process. It changes **no** packet firewall rules or network settings. It cannot check access-point WAN links, tablet alternate connections, third-party packet forwarding or previously leaked bytes.

## Risks and limitations

- Lumen intentionally serves viewer/signaling via **plain HTTP** over the LAN; WebRTC video/audio is transported with SRTP. Use only an isolated, trusted, verified local network. An untrusted managed tablet and any other LAN host may make web requests to the viewer service.
- Lumen shares **Mac system audio**, potentially including notifications and other apps; mute/close private activity. Prefer sharing only a browser window and use a separate disposable browser profile without passwords or logged-in personal accounts.
- Lumen is a relatively new third-party project, and the actual binary, macOS permissions, browser codec support and Chrome/Android WebRTC connection must be tested on the Mac and tablet. This script does not assert performance or security certification.
- **Viewing only is not zero-risk**: the tablet can signal to the Mac's local services. Host approval helps but is not a data-loss guarantee. Native streaming is safer than exposing unrestricted desktop control, but it does not guarantee Halo cannot infer or transmit anything.
- It does not turn the managed tablet into a full browser. User-driven searching from tablet needs a separate, restricted, reviewed interaction protocol—not an unrestricted remote-control socket.
- Stop with Ctrl+C and confirm the Lumen process has exited.

If browser video cannot be viewed on the dedicated LAN, do not switch to your normal internet Wi-Fi to make it work. Inspect `lumen` logs, interface routing and WebRTC candidates instead.

References: https://github.com/flaviodelgrosso/lumen ; https://github.com/flaviodelgrosso/lumen#security
