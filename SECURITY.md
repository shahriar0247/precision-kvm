# Security

PrecisionKVM can capture input, inject it on a paired machine and synchronize clipboard contents. Use it only between machines you control on a trusted network. Pairing requires a certificate fingerprint and a random token; the client must never fall back to an unverified server.

Only the latest source revision and published release receive fixes. This project is early software and has not had a comprehensive independent security audit. Checksums identify downloaded bytes only when the checksum's source is trusted; the provided SHA256 files do not replace code signing.

To report a suspected vulnerability privately, open the repository's **Security → Advisories → Report a vulnerability** form when enabled. If the form is unavailable, create a minimal issue requesting a private reporting channel without including exploit details, credentials or personal data. Please include the affected version, platform, impact, a safe reproduction and suggested mitigation.

Do not attach your pairing file, server private key, real clipboard contents, account passwords or logs containing them. Keep test listeners bound to localhost and use generated identities. Do not test against another person's machine without permission.

Important boundaries:

- Authentication precedes input capture and clipboard transfer.
- Input and clipboard connections have separate queues and size limits.
- Pairing tokens and TLS identities stay in each user's local state directory.
- Disconnects release injected keys/buttons; emergency stop disables the server rather than immediately reconnecting capture.
- Clipboard synchronization can disclose copied secrets to the paired machine. Disable it whenever that is undesirable.

macOS native bundles currently have no Developer ID signing or notarization, and Windows bundles have no Authenticode signature. Do not treat an unsigned release as verified by the operating system. Native OS privacy protections still apply and are not bypassed by this application.
