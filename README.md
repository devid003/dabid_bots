# Discord Image Bot + Admin Dashboard

One program runs both the Discord bot and the website (dashboard).

- Add **server IDs** and approve **members** from the panel. Nobody else can use the bot.
- Anyone in an approved server who tries a command is added to a **waiting list**. You press Approve or Deny.
- **Usage report**: per user (total, ok, errors, average time, first/last use) plus a full log with times, prompts and CSV export.
- Automations (`/autoimage`) are stored in the database, so they survive restarts.

## 1. Run it on your PC first

1. Put these files in `D:\Discord Project` (replace the old `bot.py`).
2. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` (new token if the old one was shared)
   - `ADMIN_PASSWORD` (choose a strong one; it protects the dashboard)
   - `OWNER_IDS` (your own Discord user ID, optional but handy)
   - `POLLINATIONS_TOKEN` (your new key)
3. Install and start:
   ```
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python app.py
   ```
4. Open **http://localhost:8000**, log in, and under **Approved servers** add your server ID.
5. In Discord, run `/imagine a cat in space`.

If a package fails to install on Python 3.15, install Python 3.12 or 3.13 and recreate `.venv`.

## 2. Deploy on Render

### Important facts first
- **Free Render web services sleep after 15 minutes without inbound traffic.** When asleep, the bot goes offline too. Free apps get 750 free hours per month.
- **Free instances can be kept awake** by pinging `/health` every 5 minutes (see step 6). This works as a workaround but is not guaranteed.
- **The reliable way to stay 24/7 is the Starter instance (about $7/month)**, which doesn't sleep.
- **Don't use Render's free database for this.** Free Postgres expires after a limited time (30 days according to recent sources). Use a free external Postgres such as Neon or Supabase, or a paid Render database. Check their current free limits.
- Render's file system is wiped on restart, so a database is required. Without `DATABASE_URL` all your data is lost.

### Steps
1. **Database.** Create a free Postgres at neon.tech (or supabase.com). Copy the connection string (starts with `postgresql://`).
2. **GitHub.** Create a **private** repository, then in PowerShell:
   ```
   cd "D:\Discord Project"
   git init
   git add .
   git commit -m "first commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_NAME/YOUR_REPO.git
   git push -u origin main
   ```
   `.gitignore` already keeps `.env` out. Never upload your tokens.
3. **Render.** Go to render.com → **New + → Web Service** → connect your repo, then set:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt && pip install psycopg2-binary`
   - **Start Command:** `uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
   - **Instance type:** Free to test, Starter ($7) for always-on
   - **Health Check Path:** `/health`
4. **Environment variables** (Environment tab):
   - `PYTHON_VERSION` = `3.12.7`
   - `DISCORD_TOKEN`, `ADMIN_PASSWORD`, `POLLINATIONS_TOKEN`, `OWNER_IDS`
   - `DATABASE_URL` = the Neon connection string
   - `SECRET_KEY` = any long random text
   - `DISPLAY_TZ` = `Asia/Manila`
5. **Deploy.** When the log shows `Bot logged in as ...`, open your `https://your-app.onrender.com` address and log in. Add your server ID again (the Render database is a new, empty one).
6. **Keep the free plan awake (optional).** Make a free account at uptimerobot.com and add an HTTP monitor for `https://your-app.onrender.com/health` every 5 minutes.

### Adding a new server later
1. Dashboard → **Approved servers** → add the server ID.
2. Invite the bot to that server (OAuth2 URL Generator link). If the bot joins a server that isn't approved, it leaves automatically.
3. Commands appear within about 30 seconds.

### Approving members
Anyone in an approved server who runs a command gets a "waiting for approval" message and shows up under **Waiting for approval**. Press **Approve**. You can also pre-approve by user ID.

## Security notes
- Dashboard login has a CSRF token, a signed cookie, and a lockout after 10 wrong passwords per 5 minutes.
- Secrets live only in `.env` or Render's environment variables, never in the code.
- Use a strong `ADMIN_PASSWORD`. Anyone with it controls who can use your bot.
