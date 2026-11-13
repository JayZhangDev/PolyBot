# Keeping PolyBot running

PolyBot only collects data while the computer it runs on is awake. An hour of sleep is an hour of missing order books, trades and snapshots, and the calibration numbers are only as good as that record. There are three ways to keep it going. Pick one.

| Option | Cost | Choose it when |
|---|---|---|
| [A small cloud server with Docker](#option-1-a-cloud-server-with-docker) | about $5 a month | you want data around the clock, even when your own computer is off |
| [The Windows desktop](#option-2-the-windows-desktop) | free | the PC can stay on and plugged in (this is how PolyBot runs today) |
| [A Mac with launchd](#option-3-a-mac-with-launchd) | free | the Mac can stay on and plugged in |

Each copy of PolyBot keeps its own data and its own PAPER account. Running two at once does no harm, but it doubles the requests sent to Polymarket and splits the record in two. When you move to a server, pause the Windows tasks (`Get-ScheduledTask PolyBot-* | Disable-ScheduledTask`). The Hong Kong island page reads files on the PC, so it will then show PolyBot as stopped.

All three run the same seven jobs on the same timetable. On Windows they are seven scheduled tasks; everywhere else one process, `polybot run`, does the same work:

```
$ polybot run --plan
PolyBot scheduler at 2026-11-13 03:31 UTC: what runs and when

  stream    all the time; started again if it stops
  discover  every 15 min at :07, :22, :37, :52        next 03:37, time limit 14 min
  pipeline  hourly at :05                             next 04:05, time limit 30 min
  agent     every 10 min at :00, :10, ...             next 03:40, time limit 9 min
  paper     every 5 min at :03, :08, ...              next 03:33, time limit 4 min
  rollup    daily at 02:10 UTC                        next 02:10 tomorrow, time limit 60 min
  kalshi    every 10 min at :04, :14, ...             next 03:34, time limit 9 min
```

Each job runs as its own process, so one crash cannot stop the rest. A job that missed its turn while the machine was off runs once when it comes back.

## Option 1: a cloud server with Docker

A cloud server (a "VPS", or virtual private server) is a small Linux computer in a data centre that never sleeps. Docker packs PolyBot and everything it needs into one "image", so the server needs nothing installed except Docker itself.

**What to rent.** Ubuntu 24.04, 1 CPU, 1 to 2 GB of memory and at least 25 GB of disk. Hetzner, DigitalOcean, Vultr and others sell this for about $4 to $6 a month (check the current price). Add your SSH key when you create it, so you log in with a key instead of a password.

What PolyBot needs, measured on the PC in November 2026:

- **Disk:** about 60 MB a day, so roughly 22 GB a year. About 22 MB is the day's order books and trades after the nightly packing. The other 30 to 40 MB is snapshots in the SQLite database, which keeps them even after they are copied to Parquet. A 25 GB disk lasts about a year. `polybot health` shows the use per day.
- **Memory:** the streamer uses about 90 MB and each job about 80 MB while it runs. The Kalshi job's once-a-day pairing peaks near 400 MB, which is why a 1 GB server needs the swap file below.
- **Network:** one websocket connection and a few hundred small requests an hour.

**Set it up** (one time). Connect with `ssh root@YOUR_SERVER_IP`, then:

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-v2 git
ufw allow OpenSSH && ufw --force enable        # firewall: only SSH comes in; PolyBot needs no open ports

git clone https://github.com/JayZhangDev/PolyBot.git
cd PolyBot
docker compose up -d --build
```

The first build takes a few minutes while it downloads Python and the libraries. After that the container starts again by itself after a crash or a server reboot (`restart: unless-stopped` in [`compose.yaml`](../compose.yaml)).

On a server with only 1 GB of memory, add a 2 GB swap file once. It is disk space the system can use as extra memory when it runs short.

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

**Day to day**, from the `PolyBot` folder on the server:

| To | Run |
|---|---|
| check it is collecting data | `docker compose exec polybot polybot health` |
| see the PAPER account | `docker compose exec polybot polybot account` |
| see what runs when | `docker compose exec polybot polybot run --plan` |
| watch the jobs | `docker compose logs -f --tail 50` (Ctrl+C stops watching, not PolyBot) |
| stop / start | `docker compose stop` / `docker compose start` |
| update to the latest code | `git pull && docker compose up -d --build` |
| change a setting | edit `config.yaml`; each job reads it when it next starts (`docker compose restart` for stream settings) |

`docker compose ps` shows `healthy` or `unhealthy`. It is unhealthy when no market data has arrived for 30 minutes.

**Where the data is.** Everything PolyBot keeps (both databases, the recorded data, logs and reports) is in a Docker volume called `polybot-data`. It survives stops, restarts, rebuilds and `docker compose down`. Only `docker compose down -v` deletes it. To take a copy:

```bash
docker compose cp polybot:/app/data ./backup-$(date +%F)
```

**Bringing the PC's history along (optional).** Pause the Windows tasks so the files stop changing, copy `polybot.db` and the `data` folder to the server (for example with `scp`), then, from the `PolyBot` folder there:

```bash
docker compose stop
docker compose cp ./polybot.db polybot:/app/data/polybot.db
docker compose cp ./data/. polybot:/app/data/
docker compose run --rm --user root polybot chown -R polybot:polybot /app/data   # copied files arrive owned by root
docker compose start
```

**Keeping it safe.** PolyBot has no keys, passwords or wallet, and the container accepts no connections. The server itself is the thing to look after: log in with your SSH key, leave the firewall on, and run `apt update && apt upgrade -y` now and then. Ubuntu also installs security updates by itself.

## Option 2: the Windows desktop

This is how PolyBot runs today: seven scheduled tasks that call into WSL ([`scripts/install_tasks.ps1`](../scripts/install_tasks.ps1)). They already survive a reboot and catch up on a missed run. Only three things stop them: the PC **sleeping**, the PC **shutting down**, or you **signing out**. You change these settings yourself:

1. **No sleep while plugged in.** Settings > System > Power & battery > Screen, sleep & hibernate timeouts. Set the "plugged in" sleep time to **Never**. The screen may still turn off, which does not affect PolyBot. The same from PowerShell:

   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```

2. **A laptop: closing the lid.** Control Panel > Power Options > Choose what closing the lid does. Set "Plugged in" to **Do nothing**.
3. **Stay signed in.** Locking the screen (Windows key + L) is fine. Signing out stops the tasks until you sign in again.
4. **Restarts for updates.** Settings > Windows Update > Advanced options > Active hours. Restarts then happen outside the hours you use the PC. After any restart the tasks start again when you sign in.

To check it is working, run `powershell -ExecutionPolicy Bypass -File scripts\health.ps1`. "Longest gap, last 24h" should stay under about 15 minutes. A gap of several hours means the PC slept.

Docker Desktop for Windows can also run the Option 1 container. It adds nothing here, though, because it stops when the PC sleeps too.

## Option 3: a Mac with launchd

launchd is the part of macOS that starts programs at login and keeps them running. [`scripts/com.jayzhang.polybot.plist`](../scripts/com.jayzhang.polybot.plist) tells it to run `polybot run` and to start it again if it ever stops.

```bash
git clone https://github.com/JayZhangDev/PolyBot.git && cd PolyBot
python3 -m venv .venv && .venv/bin/pip install -e .

cp scripts/com.jayzhang.polybot.plist ~/Library/LaunchAgents/
open -e ~/Library/LaunchAgents/com.jayzhang.polybot.plist    # replace /path/to/PolyBot (four places) with this folder: run `pwd`
launchctl load -w ~/Library/LaunchAgents/com.jayzhang.polybot.plist
```

| To | Run |
|---|---|
| check it is collecting data | `.venv/bin/python -m polybot health` |
| check launchd is running it | `launchctl list \| grep polybot` (a number in the first column is its process ID) |
| stop it (the jobs stop politely) | `launchctl unload -w ~/Library/LaunchAgents/com.jayzhang.polybot.plist` |
| start it again, or reload after editing the file | the `launchctl load -w` line above |

The plist passes `--keep-awake`, which stops the Mac sleeping while PolyBot runs and the charger is plugged in. On battery it sleeps as usual. Closing a MacBook's lid still puts it to sleep unless an external display is connected. Logs are in `logs/` as on the other systems. `launchd.out.log` and `launchd.err.log` in the project folder only catch a crash that happens before logging starts.
