# Helix landing site

Static one-page landing site for Helix. Deployed to Vercel.

## Files

- `index.html` — the whole page
- `styles.css` — dark theme matching the app
- `favicon.svg` — Helix Orbit Pulse mark
- `vercel.json` — caching + security headers

## Deploy to Vercel

### Option A — Dashboard (easiest)

1. Push this folder to the GitHub repo (already in `website/`).
2. Go to https://vercel.com/new and import `TW-10517/HealthAgent`.
3. Set **Root Directory** to `website`.
4. Framework preset: **Other** (it's static HTML).
5. Click **Deploy**. Vercel assigns a `.vercel.app` URL within ~30 s.
6. Add a custom domain in Settings → Domains if you have one.

### Option B — CLI

```bash
npm i -g vercel
cd website
vercel deploy --prod
```

## Updating the download link

The site links to the GitHub Releases download URL:

```
https://github.com/TW-10517/HealthAgent/releases/latest/download/Helix.exe
```

That URL always resolves to the newest release asset named `Helix.exe` — no site redeploy needed when you publish a new version. Just upload the fresh `Helix.exe` to a new GitHub Release and the "Download" button serves it immediately.

## Updating version text

Edit `index.html`:

- `<strong id="hero-version">1.0.0</strong>` in the hero note
- the `~32 MB` size in both the hero note and the download card
