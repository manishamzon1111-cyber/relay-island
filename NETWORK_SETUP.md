# Network isolation: hardware verification required

**Do not connect the managed LAVA tablet to an internet-enabled Wi-Fi network for this experiment.** The application is not a firewall, DNS filter, VPN, or MDM-removal tool.

Recommended setup: a dedicated access point with WAN physically disconnected and no upstream Wi-Fi repeater, plus a separate connection for the Mac to obtain internet. Connect the Mac to the offline access point through a dedicated Ethernet interface. Assign a static IPv4 such as `192.168.50.2/24` on that LAN interface; configure no router/gateway for that interface. The tablet joins the access point. Ensure DHCP if used supplies local addressing only and no usable public gateway. The macOS app refuses to bind to a LAN interface when forwarding is enabled in the standard macOS kernel flags; it cannot verify AP configuration or independent tablet connectivity.

For the CLI use `python3 -m relay_web --lan-host 192.168.50.2 --interface en7`, replacing `en7` with the verified dedicated Mac LAN interface. Run `ifconfig` to find the interface. **Do not run with the Mac's ordinary internet Wi-Fi interface**. Only the Mac should have an independent uplink. Do not enable Internet Sharing.

Before using a managed tablet, independently verify routing and packet forwarding using a separate test device and packet capture. Recheck after network changes and system restarts. If the managed tablet can reach a public IP or domain independently of the Mac application, the network is not isolated. Turning on a hotspot typically shares internet, so it is not automatically safe.

The app's runtime preflight only detects a subset of unsafe local configurations. It does not configure PF, inspect AP firmware, enforce service-level restrictions, or ensure other Mac services cannot be reached. The LAN listener is still an untrusted-network-facing HTTP service. Never consider its successful preflight proof that traffic cannot leak.

For application-only testing on your Mac, run `./start.command` without any LAN flags. This binds to `127.0.0.1`, so it cannot be reached from the tablet.
