# Mood Tracker — Network Mode: Complete How-To Guide

## Overview

Network mode lets you host Mood Tracker on your PC and access it from your phone, tablet, or any other device — on your home Wi-Fi **or remotely over the internet**. It adds five security layers on top of the existing vault encryption:

1. **LAN/Tailscale binding** — only devices on your home network or your Tailscale VPN can connect
2. **HTTPS encryption** — all traffic between phone and PC is encrypted
3. **Access password** — a separate password from your vault passphrase that devices must know to connect
4. **Device whitelisting** — new devices must be approved from your PC before they can do anything
5. **Vault passphrase** — encryption at rest (unchanged)

Your vault passphrase still protects your data at rest (encryption). The access password protects who can talk to the server over the network.

---

## Starting the Server

> **Important:** Always use the virtual environment Python, not your system Python. The venv has all the dependencies (FastAPI, uvicorn, cryptography, etc.). Using bare `python` will fail with `ModuleNotFoundError`.

### Local mode (default — same as before)

```
.\venv\Scripts\python.exe run.py
```

This starts the server on `http://localhost:8000`. Only your PC can access it. Nothing changes from how it worked before.

### Network mode (for phone access)

```
.\venv\Scripts\python.exe run.py --network
```

This starts the server on `https://0.0.0.0:8000` with HTTPS. You'll see a banner like:

```
============================================================
 Network mode enabled -- HTTPS + authentication required
============================================================
 Bound to: 0.0.0.0
 Access URLs:
   https://localhost:8000
   https://192.168.4.147:8000
   https://100.121.33.100:8000

 First time? Open one of those URLs on your phone:
   1. Accept the security warning (self-signed certificate)
   2. Set an access password (10+ characters)
   3. Approve your phone from the desktop Settings panel
============================================================
```

The LAN IP addresses listed (like `192.168.4.147`) are for home Wi-Fi. The Tailscale IP (like `100.121.33.100`) works from anywhere if both devices are on Tailscale.

### If you use a VPN (Mullvad, etc.)

If you run a VPN like Mullvad on your PC, make sure "Allow local network sharing" (or "Local network bypass") is enabled in your VPN settings. This lets your phone reach the server over home Wi-Fi even while the VPN is active.

If your VPN doesn't have that option, or it doesn't work, you can still access the app through Tailscale — just use the Tailscale IP (`https://100.x.x.x:8000`) instead of your LAN IP. Tailscale traffic is routed through its own encrypted tunnel and is unaffected by your VPN.

You can also bind the server to a specific IP if needed:

```
.\venv\Scripts\python.exe run.py --network --bind 100.121.33.100
```

This makes the server only reachable at that IP. Use `--bind 0.0.0.0` (the default) to listen on all interfaces.

### Custom port

```
.\venv\Scripts\python.exe run.py --network --port 443
```

Use `--port` if port 8000 is taken or you want a different one.

### Custom bind address

```
.\venv\Scripts\python.exe run.py --network --bind 192.168.1.50
```

Use `--bind` to bind to a specific network interface. Without this flag, network mode binds to `0.0.0.0` (all interfaces) and local mode binds to `127.0.0.1` (localhost only).

---

## First-Time Setup (Step by Step)

### Step 1: Start the server in network mode

On your PC, open a terminal and run:

```
.\venv\Scripts\python.exe run.py --network
```

### Step 2: Set an access password from your PC

1. Open `https://localhost:8000` in your PC's browser.
2. You'll see the vault screen (if vault is locked, unlock it with your vault passphrase).
3. Go to the **Settings** tab.
4. Scroll down to the **Network Access** section.
5. You'll see "Network mode: On — set an access password to secure access."
6. Type an access password (minimum 10 characters) in both fields.
7. Click **Enable network access**.

> **Note:** The access password is different from your vault passphrase. The vault passphrase encrypts your database. The access password controls who can connect over the network. Use different passwords for each — that way, if someone gets your access password, they still can't read your encrypted data without the vault passphrase.

### Step 3: Open the app on your phone

1. On your phone, open your browser (Safari, Chrome, etc.).
2. Type the LAN URL from the server banner, e.g. `https://192.168.4.147:8000`
3. **Your phone will show a security warning about the certificate.** This is expected — the certificate is self-signed (created by your PC, not by a public certificate authority). Click "Advanced" or "Details" and then "Proceed" or "Accept" to continue.
   - **iPhone (Safari):** Tap "Show Details" → "Visit this website" → type the URL again if asked
   - **Android (Chrome):** Tap "Advanced" → "Proceed to 192.168.4.147 (unsafe)"
4. You'll see a **Sign In** screen.

### Step 4: Enter the access password on your phone

1. Type the access password you set in Step 2.
2. The "Device name" field will be pre-filled (e.g. "iPhone" or "Android Phone") but you can change it to anything you like (e.g. "Sarah's iPhone").
3. Click **Sign In**.

### Step 5: Approve your phone from your PC

After entering the password on your phone, the phone will show a screen that says **"Awaiting Approval"** with a spinning indicator. Now go back to your PC:

1. In the Mood Tracker app on your PC, go to **Settings**.
2. Scroll to the **Network Access** section.
3. You'll see a list of devices. Your phone will appear as **"pending"** with an amber/yellow badge.
4. Click **Approve** next to your phone's name.

### Step 6: Phone connects automatically

Your phone is polling every 5 seconds. As soon as you click Approve on your PC, the phone will:

1. Automatically get past the "Awaiting Approval" screen.
2. If your vault is locked, you'll see the vault unlock screen — enter your vault passphrase.
3. The full Mood Tracker UI loads.

You're in! You can now use Mood Tracker from your phone.

---

## Subsequent Visits from Your Phone

After the first-time setup, coming back is much simpler:

1. Open `https://192.168.4.147:8000` on your phone (or your Tailscale IP/hostname — see below).
2. Accept the certificate warning (your browser may remember it for the session).
3. Enter your access password.
4. The app loads immediately — your device is already approved.

---

## Remote Access with Tailscale

If you want to access Mood Tracker when you're **not on your home Wi-Fi** (at work, at a café, traveling), use Tailscale. It creates an encrypted mesh VPN between your devices — your phone and PC both get a `100.x.x.x` IP address, and traffic flows through Tailscale's encrypted WireGuard tunnel. The app never touches the public internet.

### What Tailscale does

- Gives your PC a stable IP address like `100.100.100.100` (always the same, even if your home IP changes)
- Gives your phone a stable IP address like `100.100.100.101`
- Optionally gives you a MagicDNS hostname like `mymachine.tail12345.ts.net`
- All traffic between devices is end-to-end encrypted with WireGuard
- No router configuration, no port forwarding, no firewall changes
- Free for personal use (up to 100 devices)

### Setting up Tailscale (one time, ~5 minutes)

1. **On your PC:**
   - Go to [tailscale.com](https://tailscale.com) and sign up (free).
   - Download and install Tailscale for Windows.
   - Log in and follow the setup prompts.
   - Once connected, click the Tailscale icon in your system tray → it shows your PC's Tailscale IP (e.g. `100.100.100.100`).

2. **On your phone:**
   - Install the Tailscale app from the App Store (iOS) or Play Store (Android).
   - Log in with the same account you used on your PC.
   - Turn on the Tailscale VPN.

3. **Configure Mood Tracker for Tailscale:**

   Start the server with `--network` as usual:
   ```
   .\venv\Scripts\python.exe run.py --network
   ```

   The LAN filter accepts Tailscale IPs (the `100.64.0.0/10` range). Your Tailscale IP will appear in the startup banner alongside your LAN IPs. To access from your phone, use the Tailscale IP:
   - `https://100.121.33.100:8000`

   Or if you've enabled MagicDNS in Tailscale (in the admin console at [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns)):
   - `https://mymachine.tail12345.ts.net:8000`

### Tailscale-specific certificate notes

When you first connect via the Tailscale IP, you'll see the same self-signed certificate warning as on your LAN. Accept it once per browser session. If you enabled MagicDNS, the hostname `mymachine.tail12345.ts.net` is included in the certificate's Subject Alternative Names.

If you install Tailscale _after_ generating the certificate, you need to regenerate it to include the Tailscale IP:
1. Stop the server.
2. Delete `data/tls/cert.pem` and `data/tls/key.pem`.
3. Start the server with `.\venv\Scripts\python.exe run.py --network` — it auto-generates a new cert that includes the Tailscale IP.

### Security of Tailscale vs. LAN

| Aspect | Home Wi-Fi only | With Tailscale |
|--------|-----------------|-----------------|
| Who can connect | Anyone on your Wi-Fi | Only devices on your Tailscale network |
| Encryption | HTTPS (self-signed cert) | WireGuard (Tailscale) + HTTPS |
| Exposure to internet | None | None — Tailscale uses relay servers, not port forwarding |
| IP addresses allowed | 10.x, 172.16-31.x, 192.168.x | Same, plus 100.64-127.x |
| Access password still required? | Yes | Yes |
| Device approval still required? | Yes | Yes |

**Tailscale is strictly more secure than Wi-Fi-only access** because:
- Your phone doesn't need to be on the same Wi-Fi network
- Traffic is end-to-end encrypted even before HTTPS
- Nobody on a shared Wi-Fi (café, hotel) can see your traffic
- The app is never reachable from the public internet — Tailscale's coordination servers only facilitate the WireGuard handshake, they don't relay your data

### Troubleshooting Tailscale

- **"I can't see my PC's Tailscale IP in the browser"** — Make sure Tailscale is connected on both devices. Try pinging the PC from your phone: `ping 100.100.100.100`.
- **"The certificate warning shows a different IP than I typed"** — The certificate includes all your IPs (LAN + Tailscale) but the browser warns because it's self-signed, not because of an IP mismatch. Accept the warning.
- **"Connection refused on Tailscale IP"** — Make sure you started with `--network`. By default, local mode only listens on 127.0.0.1, which Tailscale can't reach. Also make sure Tailscale is connected on both devices.
- **"Tailscale is connected but the page times out"** — Windows Firewall may be blocking port 8000. Add an inbound rule: `netsh advfirewall firewall add rule name="Mood Tracker" dir=in action=allow protocol=TCP localport=8000`

---

## Managing Devices

### View connected devices

Go to **Settings → Network Access → Connected Devices**. You'll see a list with:

- **Name** — the device name you entered at login
- **IP** — the IP address the device connected from
- **Status** — approved (green), pending (amber), denied (red), or revoked (red)
- **Last seen** — when the device last made a request
- **Actions** — Approve/Deny (for pending) or Revoke (for approved)

Click **Refresh** to update the list.

### Revoke a device

If you lose your phone, sell it, or want to kick someone off:

1. Go to **Settings → Network Access**.
2. Find the device in the list.
3. Click **Revoke**.
4. Confirm the revocation.

The revoked device will immediately get a "Access denied" screen on its next request. It cannot reconnect without you approving it again.

> **You cannot revoke your own device from the same device.** Use the desktop to revoke a phone, or use the desktop to revoke the desktop from a different session.

### Approve all pending at once

Each device has individual Approve/Deny buttons. There is no "approve all" button — you should verify each device individually to make sure it's yours.

---

## Changing the Access Password

1. Go to **Settings → Network Access → Change Access Password**.
2. Enter your current access password.
3. Enter your new access password (minimum 10 characters).
4. Confirm the new access password.
5. Click **Change access password**.

> **Important:** Changing the access password revokes ALL approved devices. Every device (including your phone) will need to enter the new password and be re-approved. This is intentional — if you think your access password has been compromised, change it immediately and all old sessions are invalidated.

---

## Disabling Network Access

If you no longer want to access Mood Tracker from your phone:

1. On your PC, go to **Settings → Network Access → Disable Network Access**.
2. Enter your current access password to confirm.
3. Click **Disable network access**.

This deletes the access password, removes all device records, and disables the auth system. Then restart the server without `--network`:

```
.\venv\Scripts\python.exe run.py
```

The app goes back to localhost-only mode with no passwords, no device approvals — exactly as it was before.

> **Disabling network access can only be done from localhost (your PC).** A phone cannot disable it remotely. This prevents a compromised phone session from turning off security for everyone.

---

## Troubleshooting

### "Connection refused" on my phone

- Make sure you're using `.\venv\Scripts\python.exe run.py --network` (not bare `python`).
- Make sure you're using `https://` not `http://`.
- Make sure your phone is on the same Wi-Fi network as your PC (or on Tailscale).
- Check the IP address in the server banner — it might have changed if you reconnected to Wi-Fi.
- **If you use a VPN (Mullvad, NordVPN, etc.) on your PC**, it may block LAN traffic. Make sure "Allow local network sharing" is enabled in your VPN settings, or use the Tailscale IP instead of your LAN IP.

### The certificate warning won't go away

This is normal. Self-signed certificates always trigger browser warnings because they're not issued by a public authority. You accept it once per browser session. On iPhone, you can install the certificate as trusted in Settings → General → VPN & Device Management, which stops the warning permanently.

To install the certificate on iPhone:
1. Visit the URL and accept the warning.
2. Download the certificate from `https://<your-ip>:8000` (Safari may offer to download it, or you can navigate to the cert.pem file directly from `data/tls/cert.pem` on your PC).
3. Go to Settings → General → VPN & Device Management → install the profile.
4. Go to Settings → General → About → Certificate Trust Settings → enable full trust for the certificate.

### "Too many login attempts" error

You've entered the wrong password more than 5 times in 60 seconds. Wait 60 seconds and try again. This rate limit is per IP address, so it only applies to the device making the attempts.

### My phone shows "Access denied"

Your device was revoked. Go to Settings → Network Access on your PC and approve it again, or remove the device and re-login with the access password to create a new device record.

### I forgot my access password

You can't reset it from a phone. On your PC:

1. Stop the server.
2. Delete `data/auth.json` (this removes the access password and all device records).
3. Restart with `.\venv\Scripts\python.exe run.py --network`.
4. Set a new access password from Settings → Network Access.

Your vault passphrase and encrypted data are not affected.

### The page looks weird on my phone

The app's CSS has been updated with responsive breakpoints for phones. If something looks off:

- Make sure you're using a modern browser (Safari 14+, Chrome 90+).
- Try rotating your phone to landscape and back.
- Clear your browser cache and reload.

Input fields use a 16px minimum font size to prevent iOS from zooming in when you tap them.

### My phone disconnects when I lock the screen

This is expected browser behavior. When you come back, you'll need to re-enter your access password (the session token lasts 24 hours, but browsers may clear it sooner if the tab was backgrounded for a long time). If this is annoying, you can add the page to your home screen — this creates a more persistent web app experience on both iOS and Android.

---

## Security Details

### What each layer protects against

| Layer | Protects against | Does NOT protect against |
|-------|-----------------|--------------------------|
| LAN/Tailscale IP filter | Random internet traffic, accidental exposure | Devices on your Wi-Fi or Tailscale network |
| HTTPS/TLS | Wi-Fi eavesdropping, man-in-the-middle | Stolen access password |
| Access password | Unauthorized devices on your Wi-Fi or Tailscale | Phishing (there's no domain) |
| Device whitelisting | A neighbor who somehow gets your password | Physical access to your PC |
| Vault passphrase | Someone who gets full access to the app | Nothing — this is your last line of defense |
| Rate limiting | Brute-force password guessing (5/min/IP) | Patient, slow attacks |

### What data is stored where

| File | What it contains | Who can read it |
|------|-------------------|-----------------|
| `data/auth.json` | Scrypt-hashed access password, HMAC signing keys | Only the server process |
| `data/devices.json` | Device IDs, names, IPs, approval status, last-seen timestamps | Only the server process |
| `data/tls/cert.pem` | Public TLS certificate | Anyone (it's public) |
| `data/tls/key.pem` | Private TLS key | Only the server process |
| `data/vault.json` | Encrypted vault metadata (salt, hashed recovery key) | Only the server process (with passphrase) |
| `data/mood.db` | Encrypted journal entries (when vault is enabled) | Only the server process (with passphrase) |

All these files are in `data/` and excluded from git via `.gitignore`.

### Session token details

- **Format:** `v1.<base64url(payload)>.<base64url(hmac_signature)>`
- **Payload contains:** device ID, device name, issued-at timestamp, expiry timestamp, random nonce
- **Signing:** HMAC-SHA256 with a 256-bit key stored in `data/auth.json`
- **Lifetime:** 24 hours from issue, auto-renewed on each request
- **Storage:** In the browser's `localStorage` under key `mt_session_token`
- **Transmission:** `Authorization: Bearer <token>` header on every API request
- **Renewal:** The server sends a fresh token in the `X-Renewed-Session` response header; the SPA captures it and stores it automatically

### Why self-signed certificates

Mood Tracker runs on your home network, which doesn't have a public domain name. Public certificate authorities (like Let's Encrypt) only issue certificates for domains you control, not for arbitrary LAN IP addresses. Self-signed certificates provide the same encryption as public ones — the only difference is that your browser doesn't automatically trust them, so you see a warning. The encryption is equally strong.

If you want to avoid the browser warning permanently, you can:
1. Install the certificate on each device (see "Troubleshooting" above).
2. Or set up a local DNS name (e.g. `moodtracker.local`) and use `mkcert` to generate a locally-trusted certificate.

---

## Stopping the Server

Press `Ctrl+C` in the terminal where `run.py` is running. This gracefully shuts down the server, unloads Whisper and Ollama models, and closes the database.

If you close the terminal window without pressing Ctrl+C, the signal handlers will attempt a clean shutdown. On Windows, closing the window sends a `CTRL_CLOSE_EVENT` which is handled.

---

## Command Reference

| Command | Description |
|---------|-------------|
| `.\venv\Scripts\python.exe run.py` | Start in local mode (localhost only, HTTP, no auth) |
| `.\venv\Scripts\python.exe run.py --network` | Start in network mode (LAN + Tailscale, HTTPS, auth required) |
| `.\venv\Scripts\python.exe run.py --network --port 443` | Network mode on port 443 |
| `.\venv\Scripts\python.exe run.py --network --bind 100.121.33.100` | Network mode bound to a specific interface (e.g. Tailscale only) |
| `.\venv\Scripts\python.exe run.py --port 9000` | Local mode on port 9000 |

---

## Architecture Summary

```
Request flow in network mode:

Phone browser (Wi-Fi or Tailscale VPN)
  │
  │ HTTPS (self-signed cert)
  │ [Optional: WireGuard encryption if using Tailscale]
  ▼
Uvicorn (0.0.0.0:8000)
  │
  ├─ LAN/Tailscale IP filter
  │   (accept: 10.x, 172.16-31.x, 192.168.x, 100.64-127.x, 127.x, *.ts.net)
  │   (reject: everything else)
  │
  ├─ CORS check (allow https://LAN+Tailscale origins)
  │
  ├─ Auth middleware
  │   ├─ Public paths: /auth/login, /auth/status, /vault/*, /static/*, /, /healthz
  │   ├─ CSRF check: X-Requested-With header required on POST/PUT/DELETE
  │   └─ Session token: validate HMAC-SHA256 signature, check device is approved
  │
  ├─ Vault lock middleware
  │   └─ Return 401 if vault is set up but locked (except for public paths)
  │
  ├─ Security headers (CSP, HSTS, X-Frame-Options, etc.)
  │
  └─ FastAPI route handler
```

The auth layer and vault layer are independent. Auth controls *who can connect*. Vault controls *who can read encrypted data*. You can use auth without vault (no encryption) or vault without auth (localhost only). In network mode, both are recommended.