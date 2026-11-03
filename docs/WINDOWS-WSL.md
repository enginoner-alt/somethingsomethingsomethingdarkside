# Running the POC on Windows with WSL2

These are **POC-only** instructions for running the stack on a Windows laptop
(e.g. the RTX 4060 one) through WSL2. Production runs on plain Linux and
follows the [README](../README.md).

The idea: run a real Ubuntu inside WSL2, install Docker Engine there, and from
then on follow the Linux instructions unchanged. The extra steps here cover the
parts where Windows gets in the way: networking, memory, the GPU and line endings.

> **The mic doesn't need WSL at all.** The XVF3800 (or any mic) plugs into the
> device running the *browser*. The browser records and uploads over HTTPS. The
> server in WSL never touches USB audio, so no USB passthrough is needed.

## 1. Install WSL2 + Ubuntu

In **PowerShell as Administrator**:

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot if asked, open "Ubuntu" from the Start menu, and create your Linux user.

Make sure it's WSL **2**:

```powershell
wsl -l -v
```

## 2. Give WSL enough memory and enable mirrored networking

By default WSL2 gets half your RAM (8 GB of 16 GB), which is too tight for
WhisperX + pyannote in M2. Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=12GB
swap=8GB
# Windows 11 22H2+: WSL shares the laptop's IP, so phones on your LAN can reach it.
networkingMode=mirrored
```

Apply it:

```powershell
wsl --shutdown
```

On Windows 10, or if mirrored mode isn't available, see
[Fallback: port proxy](#fallback-port-proxy-windows-10--no-mirrored-mode).

## 3. Enable systemd inside Ubuntu

In the Ubuntu shell:

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

```powershell
wsl --shutdown
```

Reopen Ubuntu.

## 4. Install Docker Engine inside Ubuntu

Use Docker Engine in WSL, not Docker Desktop: it's the same setup as
production. (Docker Desktop with the WSL2 backend also works if you already
have it. Skip this step and enable WSL integration for your Ubuntu distro.)

```bash
curl -fsSL https://get.docker.com | sudo sh
```

```bash
sudo usermod -aG docker $USER
```

Close and reopen the Ubuntu window, then check:

```bash
docker run --rm hello-world
```

## 5. Clone the repo *inside* the Linux filesystem

Clone into your Linux home (`~`), **not** under `/mnt/c/...`. Docker on
`/mnt/c` is slow and file permissions break.

```bash
cd ~ && git clone https://github.com/enginoner-alt/somethingsomethingsomethingdarkside.git
```

```bash
cd ~/somethingsomethingsomethingdarkside
```

Shell scripts are pinned to LF line endings by `.gitattributes`. Still, don't
clone with Git for Windows and copy the folder over: CRLF line endings make the
containers fail with `exec ... no such file or directory`.

## 6. Configure and start

Follow **Run it** in the [README](../README.md): `.env`, certificates, and
`docker compose up -d --build`.

Your wildcard cert files are probably on the Windows side. Copy them in from
there (Windows drives are under `/mnt/c`):

```bash
cp /mnt/c/Users/<you>/path/to/fullchain.pem /mnt/c/Users/<you>/path/to/privkey.pem deploy/certs/
```

## 7. Make the hostname resolve

`SITE_ADDRESS` (e.g. `rec.example.com`) must resolve to the laptop.

- **Only the laptop's own browser:** add a line to
  `C:\Windows\System32\drivers\etc\hosts` (edit as Administrator):

  ```
  127.0.0.1  rec.example.com
  ```

- **Phones and other devices on your LAN:** they can't use the laptop's hosts
  file. Add a DNS record pointing the hostname at the laptop's **LAN IP** (find
  it with `ipconfig`). Put the record in your router, Pi-hole or internal DNS,
  or as a public A record with a private IP. Some routers block that last
  option ("DNS rebinding protection"). If so, whitelist the domain or use
  internal DNS.

Give the laptop a fixed LAN IP (a DHCP reservation in the router) so the record
stays valid.

## 8. Open the firewall (for LAN access)

In **PowerShell as Administrator**:

```powershell
New-NetFirewallRule -DisplayName "Darkside HTTP/HTTPS" -Direction Inbound -Protocol TCP -LocalPort 80,443 -Action Allow -Profile Private
```

With mirrored networking, WSL also sits behind the Hyper-V firewall. Allow the
same ports there:

```powershell
New-NetFirewallHyperVRule -Name "DarksideWSL" -DisplayName "Darkside WSL HTTP/HTTPS" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -Protocol TCP -LocalPorts 80,443
```

Make sure your Wi-Fi network is set to **Private**, not Public, in Windows settings.

Test from a phone on the same Wi-Fi: `https://rec.example.com` should show the
login page with a valid certificate.

## 9. Keep it running

- **WSL doesn't start on boot.** Docker (and the stack, thanks to
  `restart: unless-stopped`) only runs once WSL is running. Open Ubuntu once
  after boot, or add a Task Scheduler task "At log on" running
  `wsl.exe -d Ubuntu-24.04 --exec true`.
- **Laptop sleep kills recordings.** If the laptop sleeps mid-meeting, uploads
  stop. Set "When plugged in, put my device to sleep" to **Never** while testing.
- **Ports 80/443 already in use?** Something on Windows (IIS, Skype, another
  web server) may hold them. Find the culprit with
  `netstat -ano | findstr ":443"`.

## 10. GPU check (needed from M2)

Install only the **Windows** NVIDIA driver (the normal Game Ready / Studio
driver). **Never install a Linux NVIDIA driver inside WSL**: the Windows driver
provides the GPU to WSL.

In Ubuntu:

```bash
nvidia-smi
```

That should list the RTX 4060. At this point WSL has already given the GPU to
Linux programs. **Docker containers** are a separate layer, though:

- **Docker Desktop (WSL2 backend):** GPU support for containers is built in.
  Skip to the check below; no toolkit needed.
- **Docker Engine inside WSL (step 4):** containers can't see the GPU until you
  install the NVIDIA Container Toolkit. Without it, `--gpus all` fails with
  `could not select device driver "" with capabilities: [[gpu]]`.

Not sure which case you're in? Run the check below first. If it prints the
4060, you're done. If it shows that error, install the toolkit
(official guide: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>,
the "apt" section) and configure Docker:

```bash
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

Confirm containers can see the GPU:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

If that prints the 4060, you're ready for M2.

## Running the tests in WSL (optional, for development)

```bash
docker run -d --name darkside-testdb -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test -p 5432:5432 postgres:16-alpine
```

```bash
sudo apt install -y python3-venv && cd server && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

```bash
TEST_DATABASE_URL=postgresql+psycopg://test:test@localhost:5432/test pytest
```

## Fallback: port proxy (Windows 10 / no mirrored mode)

Without mirrored networking, WSL2 sits behind NAT. Windows forwards
`localhost` to it automatically, but other devices can't reach it. Forward the
ports manually (PowerShell as Administrator). The WSL IP changes on every
restart, so re-run this after each one:

```powershell
$ip = (wsl hostname -I).Trim().Split(" ")[0]
```

```powershell
foreach ($p in 80,443) { netsh interface portproxy delete v4tov4 listenport=$p listenaddress=0.0.0.0 2>$null; netsh interface portproxy add v4tov4 listenport=$p listenaddress=0.0.0.0 connectport=$p connectaddress=$ip }
```

Then add the normal Windows firewall rule from step 8. You don't need the
Hyper-V rule in this mode.

## Moving to production later

Nothing in the repo is WSL-specific. On the production Linux server, follow the
README directly. Copy `.env` and `deploy/certs/` over. Move data by dumping and
restoring Postgres (`docker compose exec postgres pg_dump ...`), and from M1 on
also copy the audio volume.
